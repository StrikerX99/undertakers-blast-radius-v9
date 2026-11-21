"""Observe class and instance reads of leaf expression metadata during native tests."""

import atexit
import builtins
from collections import defaultdict
import json
import os
from pathlib import Path
from types import MappingProxyType


def install():
    import profile_pool

    profile_pool.install()
    state = Path(os.environ["BR_EXPR_STATE"])
    ref = json.loads((state / "targets.json").read_bytes())
    pid = str(os.getpid())
    (state / (pid + ".start")).touch()
    seen = defaultdict(lambda: defaultdict(set))
    installed = set()
    parents = {}
    invalid = set()
    watches = {}
    fields = {"arg_types": "schema", "required_args": "schema", "__doc__": "doc"}
    original_build = builtins.__build_class__
    import importlib._bootstrap_external as machinery

    loader = machinery._LoaderBasics
    old_exec = loader.exec_module
    importing = 0

    def traced_exec(self, module):
        nonlocal importing
        importing += 1
        try:
            return old_exec(self, module)
        finally:
            importing -= 1

    loader.exec_module = traced_exec

    def observe(key, group):
        context = profile_pool.context()
        test, _, phase = context.rpartition(" (")
        if importing or not test or phase != "call)":
            test = "*"
        seen[key][group].add(test)

    def inherited(cls, name):
        for ancestor in type.__getattribute__(cls, "__mro__")[1:]:
            if name in type.__getattribute__(ancestor, "__dict__"):
                return type.__getattribute__(ancestor, name)
        raise AttributeError(name)

    class Resolved:
        def __init__(self, key, group, value, name, base):
            self.key, self.group, self.value = key, group, value
            self.name, self.base = name, base

        def __get__(self, obj, owner=None):
            observe(self.key, self.group)
            if self.base is not None:
                actual = inherited(self.base, self.name)
                if actual is not self.value:
                    invalid.add(self.key)
                return actual
            return self.value

    class Meta(type):
        def __getattribute__(cls, name):
            key = watches.get(cls)
            if key is not None and name == "__dict__":
                invalid.add(key)
                # Restore the namespace visible in a pristine class. Inherited
                # bindings were shadowed by tracing descriptors only.
                actual = type.__getattribute__(cls, "__dict__")
                ns = dict(actual)
                for f in fields:
                    val = ns.get(f)
                    if isinstance(val, Resolved):
                        if f in originals[cls]:
                            ns[f] = val.value
                        else:
                            ns.pop(f)
                return MappingProxyType(ns)
            return type.__getattribute__(cls, name)

        def __setattr__(cls, name, value):
            if name in fields and cls in watches:
                invalid.add(watches[cls])
            return type.__setattr__(cls, name, value)

        def __delattr__(cls, name):
            if name in fields and cls in watches:
                invalid.add(watches[cls])
            return type.__delattr__(cls, name)

    # inspect.getattr_static bypasses ordinary descriptors and the metaclass.
    # Keep any class subject to direct dictionary reflection conservative.
    import inspect

    old_static = inspect.getattr_static

    def static(obj, name, *args):
        cls = obj if type(obj) is Meta else type(obj)
        if cls in watches and (name in fields or name == "__dict__"):
            invalid.add(watches[cls])
        result = old_static(obj, name, *args)
        return result.value if isinstance(result, Resolved) else result

    inspect.getattr_static = static

    originals = {}

    def build(fn, name, *bases, **kwargs):
        # Runtime-created descendants could observe the binding through an
        # additional inheritance path. Keep their ancestors conservative.
        for base in bases:
            if type(base) is Meta:
                key = watches.get(base)
                if key:
                    invalid.add(key)
        module = fn.__module__
        if name not in ref.get(module, {}) or fn.__qualname__ != name:
            return original_build(fn, name, *bases, **kwargs)
        key = module + "." + name
        if (
            kwargs
            or not bases
            or any(type(base) is not type for base in bases)
            or any(
                "__init_subclass__" in vars(parent)
                and (parent.__module__, parent.__name__)
                not in {("builtins", "object"), ("sqlglot.expressions.core", "Expr")}
                for base in bases
                for parent in base.__mro__
            )
            or ("sqlglot.expressions.core", "Expr")
            not in {(p.__module__, p.__name__) for base in bases for p in base.__mro__}
        ):
            invalid.add(key)
            return original_build(fn, name, *bases, **kwargs)
        cls = original_build(fn, name, *bases, metaclass=Meta)
        ns = type.__getattribute__(cls, "__dict__")
        orig = set(ns)
        values = {f: type.__getattribute__(cls, f) for f in fields}
        if (
            type(values["arg_types"]) is not dict
            or type(values["required_args"]) is not set
            or any(
                type(k) is not str or type(v) is not bool
                for k, v in values["arg_types"].items()
            )
            or type(values["__doc__"]) is not str
        ):
            invalid.add(key)
            return cls
        # Only immutable primitive keys and boolean values are eligible. Any
        # observed mutation/rebinding of a shared table invalidates the class.
        for f, v in values.items():
            type.__setattr__(
                cls, f, Resolved(key, fields[f], v, f, None if f in orig else cls)
            )
        originals[cls] = orig
        watches[cls] = key
        parents[cls] = (
            values,
            tuple(values["arg_types"].items()),
            set(values["required_args"]),
            orig,
        )
        installed.add(key)
        return cls

    builtins.__build_class__ = build

    @atexit.register
    def save():
        for cls, (values, items, required, own) in parents.items():
            key = watches[cls]
            if (
                tuple(values["arg_types"].items()) != items
                or values["required_args"] != required
            ):
                invalid.add(key)
            if (
                "arg_types" not in own
                and inherited(cls, "arg_types") is not values["arg_types"]
            ):
                invalid.add(key)
        d = {
            "installed": sorted(installed),
            "invalid": sorted(invalid),
            "seen": {
                k: {g: sorted(ts) for g, ts in row.items()} for k, row in seen.items()
            },
        }
        (state / (pid + ".done")).write_text(json.dumps(d))

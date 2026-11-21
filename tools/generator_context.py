"""Observe shared SQLGlot generator settings, method calls and namespace inspection."""

import atexit
import builtins
from collections import defaultdict
import importlib._bootstrap_external
import inspect
import json
import os
from pathlib import Path
import sys
import threading
from types import MappingProxyType

import profile_pool
from generator_namespaces import ClassNamespace


def pool_task(owner, function, args, kwargs):
    old = profile_pool._task_context
    profile_pool._task_context = owner
    sys.monitoring.restart_events()
    try:
        return function(*args, **kwargs)
    finally:
        profile_pool._task_context = old
        sys.monitoring.restart_events()


def install():
    profile_pool._run = pool_task
    profile_pool.install()
    state = Path(os.environ["BR_GENERATOR_DATA"])
    root = os.environ["BR_GENERATOR_PIN"]
    (state / f"{os.getpid()}.started").touch()
    base = root + "/sqlglot/generator.py"
    dialects = root + "/sqlglot/generators/"
    core = None
    known = set()
    observed = defaultdict(set)
    main_thread = threading.get_ident()
    depth = 0
    loader = importlib._bootstrap_external._LoaderBasics
    exec_module = loader.exec_module

    def module(self, mod):
        nonlocal depth
        depth += 1
        try:
            return exec_module(self, mod)
        finally:
            depth -= 1

    loader.exec_module = module

    def context():
        if depth or threading.get_ident() != main_thread:
            return "*"
        name, _, phase = profile_pool.context().rpartition(" (")
        if not name:
            return "*"
        return name if phase == "call)" else name.partition("::")[0] + "::*"

    class Value:
        def __init__(self, name, value):
            self.name, self.value = name, value

        def __get__(self, instance, owner):
            observed["value:" + self.name].add(context())
            return self.value

    dir_depth = 0

    class Metaclass(type):
        def __dir__(cls):
            nonlocal dir_depth
            dir_depth += 1
            try:
                return type.__dir__(cls)
            finally:
                dir_depth -= 1

        def __getattribute__(cls, key):
            if key == "__dict__" and cls is core:
                if dir_depth:
                    observed["namespace-keys-only"].add(context())
                else:
                    observed["inspection"].add(context())
                values = type.__getattribute__(cls, key)
                if os.environ.get("BR_NAMESPACE_KEYS"):

                    def report(name):
                        if not dir_depth and name in known:
                            observed["namespace:" + name].add(context())

                    return MappingProxyType(
                        ClassNamespace(
                            values,
                            lambda v: v.value if isinstance(v, Value) else v,
                            report,
                        )
                    )
                return MappingProxyType(
                    {
                        n: v.value if isinstance(v, Value) else v
                        for n, v in values.items()
                    }
                )
            return type.__getattribute__(cls, key)

        def __setattr__(cls, key, val):
            if cls is core and key in known:
                observed["value:" + key].add("*")
            return type.__setattr__(cls, key, val)

        def __delattr__(cls, key):
            if cls is core and key in known:
                observed["value:" + key].add("*")
            return type.__delattr__(cls, key)

    builder = builtins.__build_class__

    def build(body, name, *bases, **kwargs):
        nonlocal core
        if (
            name != "Generator"
            or body.__module__ != "sqlglot.generator"
            or body.__qualname__ != "Generator"
        ):
            return builder(body, name, *bases, **kwargs)
        if bases or kwargs:
            raise ValueError("Unexpected Generator declaration")
        cls = builder(body, name, metaclass=Metaclass)
        for n, v in list(type.__getattribute__(cls, "__dict__").items()):
            if n.isupper() and type(v) in (str, int, float, bool, type(None)):
                known.add(n)
                type.__setattr__(cls, n, Value(n, v))
        core = cls
        return cls

    builtins.__build_class__ = build
    old_static = inspect.getattr_static

    def static(obj, name, *args):
        v = old_static(obj, name, *args)
        if isinstance(v, Value):
            observed["value:" + v.name].add(context())
            return v.value
        return v

    inspect.getattr_static = static
    mon = sys.monitoring
    tool = mon.PROFILER_ID
    mon.use_tool_id(tool, "generator method contexts")

    def start(code, offset):
        p = code.co_filename
        if (
            (p == base or p.startswith(dialects))
            and "." in code.co_qualname
            and "<" not in code.co_qualname
        ):
            key = p[len(root) + 1 :] + ":" + code.co_qualname
            observed[key].add(context())
        return mon.DISABLE

    mon.register_callback(tool, mon.events.PY_START, start)
    if not os.environ.get("BR_NAMESPACE_ONLY"):
        mon.set_events(tool, mon.events.PY_START)

    @atexit.register
    def save():
        (state / f"{os.getpid()}.done").write_text(
            json.dumps(
                {
                    "settings": sorted(known),
                    "events": {k: sorted(ts) for k, ts in observed.items()},
                }
            )
        )

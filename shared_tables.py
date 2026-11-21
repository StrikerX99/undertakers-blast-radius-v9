"""Compare shared SQLGlot lookup tables after clean and patched dialect initialization."""

from enum import Enum
from functools import lru_cache
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import BuiltinFunctionType, CodeType, FunctionType, MethodType

import forkprobe
from workers import schedule


@lru_cache(maxsize=None)
def code_signature(code):
    fields = [
        code.co_code.hex(),
        code.co_exceptiontable.hex(),
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        code.co_flags,
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        [encode(c) for c in code.co_consts],
    ]
    return hashlib.sha256(
        json.dumps(fields, separators=(",", ":")).encode()
    ).hexdigest()


def encode(value):
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, Enum):
        return ["enum", type(value).__module__, type(value).__qualname__, value.name]
    if isinstance(value, type):
        return ["type", value.__module__, value.__qualname__]
    if isinstance(value, CodeType):
        return ["code", code_signature(value)]
    if value is Ellipsis:
        return ["ellipsis"]
    if type(value) is bytes:
        return ["bytes", value.hex()]
    if isinstance(value, MethodType):
        return ["bound", encode(value.__func__), encode(value.__self__)]
    if isinstance(value, FunctionType):
        return [
            "fn",
            value.__module__,
            value.__qualname__,
            code_signature(value.__code__),
            encode(value.__defaults__),
            encode(value.__kwdefaults__),
            [encode(c.cell_contents) for c in value.__closure__ or ()],
        ]
    if isinstance(value, BuiltinFunctionType):
        return ["builtin", value.__module__, value.__qualname__]
    if type(value) is dict:
        return ["dict", [[encode(k), encode(v)] for k, v in value.items()]]
    if type(value) in (list, tuple):
        return [type(value).__name__, [encode(v) for v in value]]
    if type(value) in (set, frozenset):
        return [type(value).__name__, sorted(json.dumps(encode(v)) for v in value)]
    raise TypeError(type(value).__name__)


def fingerprint(root, module):
    os.chdir(root)
    sys.path.insert(0, str(root))
    import sqlglot
    from sqlglot import Dialect, Generator, Parser, Tokenizer, exp

    importlib.import_module("sqlglot.dialects." + module.rsplit(".", 1)[-1])
    touched = importlib.import_module(module)
    if Path(sqlglot.__file__).resolve() != (root / "sqlglot/__init__.py").resolve():
        raise ValueError("Unexpected source location")
    if (
        Path(touched.__file__).resolve()
        != (root / (module.replace(".", "/") + ".py")).resolve()
    ):
        raise ValueError("Unexpected dialect source location")
    fields = [
        (type(Dialect), ("_classes",)),
        (Parser, ("FUNCTIONS", "FUNCTION_PARSERS", "NO_PAREN_FUNCTION_PARSERS")),
        (Generator, ("TRANSFORMS", "TYPE_MAPPING")),
        (Tokenizer, ("KEYWORDS",)),
    ]
    rows = [
        [owner.__qualname__, name, encode(getattr(owner, name))]
        for owner, keys in fields
        for name in keys
    ]
    # The outer type index is constructed from sets of expression classes. Its
    # iteration order depends on addresses even across two clean Python runs.
    metadata = sorted(
        (key.__module__, key.__qualname__, encode(value))
        for key, value in Dialect.EXPRESSION_METADATA.items()
    )
    rows.append(["Dialect", "EXPRESSION_METADATA", metadata])
    for name, cls in sorted(vars(exp).items()):
        if isinstance(cls, type) and issubclass(cls, exp.Expr):
            rows.append([name, encode(cls.arg_types), encode(cls.required_args)])
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def run(payload):
    root, module, out = payload
    out.write_text(fingerprint(root, module))


def inspect(root, module, timeout):
    with tempfile.TemporaryDirectory(prefix="br-shared-") as temporary:
        dest = Path(temporary) / "state"
        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
        if forkprobe.run((root, module, dest), env, run, timeout) and dest.is_file():
            return dest.read_text()
    return None


@lru_cache(maxsize=40)
def pristine(root, module):
    return inspect(root, module, 3.0)


def verified(selector, changes, previous, proposed, checkout, baseline, remaining):
    if proposed == previous or len(changes) != 1 or remaining < 7:
        return previous
    before = schedule([selector.by_number[i] for i in previous])[1]
    after = schedule([selector.by_number[i] for i in proposed])[1]
    if before - after < 750:
        return previous
    path = changes[0]["old"]
    module = path.removesuffix(".py").replace("/", ".")
    expected = pristine(baseline, module)
    if expected is None:
        return previous
    with tempfile.TemporaryDirectory(prefix="br-dialect-") as tmp:
        root = Path(tmp) / "repo"
        checkout(root, changes, selector)
        if inspect(root, module, 3.0) == expected:
            return proposed
    return previous

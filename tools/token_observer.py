"""Collect parser token dispatch in every interpreter before SQLGlot imports."""

import ast
import atexit
import builtins
from collections import defaultdict
from functools import lru_cache
import hashlib
import importlib.abc
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import sys


def install():
    root = Path(os.environ["BR_TOKEN_ROOT"])
    directory = Path(os.environ["BR_TOKEN_TRACE"])
    hashes = json.loads((directory / "source_hashes").read_bytes())
    output = directory / f"{os.getpid()}.json"
    output.with_suffix(".start").touch()
    observations = defaultdict(set)

    @lru_cache(maxsize=None)
    def ordinary(cls):
        from sqlglot.parser import Parser

        return (
            issubclass(cls, Parser)
            and cls.__getattribute__ is object.__getattribute__
            and inspect.getattr_static(cls, "_curr") is Parser._curr
        )

    def observe(site, method, enum_class, *args, **kwargs):
        from sqlglot.parser import Parser
        from sqlglot.tokens import Token, TokenType

        native = getattr(method, "__func__", None)
        receiver = getattr(method, "__self__", None)
        key = "*"
        if (
            native is Parser._match
            and native.__code__.co_name == "_match"
            and native.__code__.co_filename == str(root / "sqlglot/parser.py")
            and enum_class is TokenType
            and type(args[0]) is TokenType
            and ordinary(type(receiver))
        ):
            current = Parser._curr.__get__(receiver)
            if type(current) is Token:
                kind = Token.token_type.__get__(current)
                if type(kind) is TokenType:
                    key = kind.name
        observations[site].add(key)
        return method(*args, **kwargs)

    class Instrument(ast.NodeTransformer):
        def __init__(self, path):
            self.path = path

        def visit_Call(self, node):
            self.generic_visit(node)
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "_match"
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and node.args
                and isinstance(node.args[0], ast.Attribute)
                and isinstance(node.args[0].value, ast.Name)
                and node.args[0].value.id == "TokenType"
            ):
                return node
            key = f"{self.path}:{node.lineno}:{node.col_offset}"
            wrapper = ast.Call(
                ast.Name("_br_match_token_", ast.Load()),
                [
                    ast.Constant(key),
                    func,
                    ast.Name("TokenType", ast.Load()),
                    *node.args,
                ],
                node.keywords,
            )
            return ast.copy_location(wrapper, node)

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, name):
            src = Path(self.path)
            path = src.relative_to(root).as_posix()
            raw = src.read_bytes()
            if hashlib.sha256(raw).hexdigest() != hashes[path]:
                raise ValueError(f"Source mismatch for {path}")
            tree = Instrument(path).visit(ast.parse(raw, str(src)))
            return compile(ast.fix_missing_locations(tree), str(src), "exec")

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "sqlglot.parser" or name.startswith("sqlglot.parsers."):
                spec = importlib.machinery.PathFinder.find_spec(name, path)
                if spec and spec.origin and Path(spec.origin).is_relative_to(root):
                    spec.loader = Loader(name, spec.origin)
                    return spec

    if "sqlglot" in sys.modules:
        raise ValueError("SQLGlot was already imported before the reference observer")
    builtins._br_match_token_ = observe
    sys.meta_path.insert(0, Finder())

    def flush():
        tmp = output.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({k: sorted(v) for k, v in observations.items()}, sort_keys=True)
        )
        tmp.replace(output)

    atexit.register(flush)

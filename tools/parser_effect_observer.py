"""Record baseline reads of parser guard operands and consumption of optional arguments."""

import importlib.util
import ast
import atexit
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import sys
import types
from collections import defaultdict
from pathlib import Path
from parser_sites import guard, optional, site


def install():
    import profile_pool

    profile_pool.install()
    root = Path(os.environ["BR_PARSE_ROOT"])
    state = Path(os.environ["BR_PARSE_STATE"])
    pid = os.getpid()
    (state / f"{pid}.start").touch()
    dirty = defaultdict(set)
    sites = set()
    match_context = []

    def mark(k):
        name, _, when = profile_pool.context().rpartition(" (")
        if not name:
            key = "*"
        elif when == "call)":
            key = name
        else:
            key = name.partition("::")[0] + "::*"
        dirty[k].add(key)

    def pure_truth(obj):
        return type(obj) in (
            bool,
            int,
            str,
            float,
            list,
            dict,
            tuple,
            set,
            frozenset,
            type(None),
        ) or (
            getattr(type(obj), "__bool__", None) is None
            and getattr(type(obj), "__len__", None) is None
        )

    def watch(k, val, parser=None, slot=None):
        if (
            not pure_truth(val)
            or slot is not None
            and (
                type(parser).__getattribute__ is not object.__getattribute__
                or type(getattr(type(parser), slot, None))
                is not types.MemberDescriptorType
            )
        ):
            mark(k)
        return val

    def wrap_match(k, fn, *args, **kwargs):
        parser = getattr(fn, "__self__", None)
        expected = module_parser()
        if type(parser).__getattribute__ is not object.__getattribute__:
            mark(k)
        # Confirm dynamic dispatch at the hook entry and both annotated implementations.
        if (
            expected is None
            or type(fn) is not types.MethodType
            or fn.__func__.__code__ is not audited.get("_match")
            or getattr(getattr(parser, "_add_comments", None), "__func__", None) is None
            or parser._add_comments.__func__.__code__
            is not audited.get("_add_comments")
        ):
            mark(k)
        if os.getpid() != int(os.environ["BR_POOL_OWNER"]):
            (state / f"{pid}.pool").touch() if not pool_observed[0] else None
            pool_observed[0] = True
        match_context.append((k, parser, kwargs["expression"]))
        try:
            return fn(*args, **kwargs)
        finally:
            match_context.pop()

    pool_observed = [False]

    def module_parser():
        mod = sys.modules.get("sqlglot.parser")
        return getattr(mod, "Parser", None)

    def add_comments(parser, expression):
        if match_context:
            key, owner, arg = match_context[-1]
            if (
                parser is not owner
                or arg is not expression
                or not pure_truth(expression)
            ):
                mark(key)
            elif expression is not None:
                if (
                    type(parser).__getattribute__ is not object.__getattribute__
                    or type(getattr(type(parser), "_prev_comments", None))
                    is not types.MemberDescriptorType
                ):
                    mark(key)
                elif type(parser._prev_comments) is not list or parser._prev_comments:
                    mark(key)

    safety_isinstance = builtins.isinstance

    def wrapped(obj, attr, val, k):
        # Preserve the shape and number of evaluations of the original conditional.
        if (
            obj is not safety_isinstance
            or type(attr) is not type
            or type(val).__getattribute__ is not object.__getattribute__
            or type(type(val)) is not type
            or next(
                (
                    t.__dict__["__class__"]
                    for t in type(val).__mro__
                    if "__class__" in t.__dict__
                ),
                None,
            )
            is not object.__dict__["__class__"]
        ):
            mark(k)
        return obj

    def flag(k):
        mark(k)

    audited = {}

    class Transform(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            if node.name == "_add_comments":
                call = ast.parse("_br_actual_comments(self,expression)").body[0]
                node.body.insert(0, ast.copy_location(call, node.body[0]))
            self.generic_visit(node)
            return node

        def visit_If(self, node):
            self.generic_visit(node)
            if not guard(node):
                return node
            k = site(node)
            sites.add(k)

            def wrap(v):
                if isinstance(v, ast.BoolOp):
                    v.values = [wrap(t) for t in v.values]
                    return v
                if isinstance(v, ast.UnaryOp):
                    v.operand = wrap(v.operand)
                    return v
                args = [ast.Constant(k), v]
                if isinstance(v, ast.Attribute):
                    args += [ast.Name(id="self", ctx=ast.Load()), ast.Constant(v.attr)]
                return ast.copy_location(
                    ast.Call(ast.Name("_br_watch", ast.Load()), args, []), v
                )

            node.test = wrap(node.test)
            node.body.insert(
                0,
                ast.copy_location(
                    ast.Expr(
                        ast.Call(
                            ast.Name("_br_flag", ast.Load()), [ast.Constant(k)], []
                        )
                    ),
                    node.body[0],
                ),
            )
            return node

        def visit_Call(self, node):
            key = optional(node)
            if key is None:
                self.generic_visit(node)
                return node
            k = site(node)
            sites.add(k)
            if isinstance(key.value, ast.IfExp):
                # Preserve isinstance dispatch. The pinned exp module's Expr type
                # and builtin isinstance have no custom instance checking.
                v = key.value.test
                v.func = ast.copy_location(
                    ast.Call(
                        ast.Name("_br_instance", ast.Load()),
                        [v.func, v.args[1], v.args[0], ast.Constant(k)],
                        [],
                    ),
                    v.func,
                )
            return ast.copy_location(
                ast.Call(
                    ast.Name("_br_match", ast.Load()),
                    [ast.Constant(k), node.func, *node.args],
                    node.keywords,
                ),
                node,
            )

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tr = ast.parse(Path(self.path).read_bytes(), self.path)
            for c in tr.body:
                if isinstance(c, ast.ClassDef) and c.name == "Parser":
                    for fn in c.body:
                        if isinstance(fn, ast.FunctionDef):
                            Transform().visit(fn)
            return compile(
                ast.fix_missing_locations(tr), self.path, "exec", dont_inherit=True
            )

        def exec_module(self, module):
            super().exec_module(module)
            for n in ("_match", "_add_comments"):
                audited[n] = getattr(module.Parser, n).__code__

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "sqlglot.parser":
                p = str(root / "sqlglot/parser.py")
                return importlib.util.spec_from_file_location(
                    fullname, p, loader=Loader(fullname, p)
                )

    builtins._br_watch = watch
    builtins._br_flag = flag
    builtins._br_match = wrap_match
    builtins._br_instance = wrapped
    builtins._br_actual_comments = add_comments
    sys.meta_path.insert(0, Finder())
    atexit.register(
        lambda: (state / f"{pid}.json").write_text(
            json.dumps([sorted(sites), {k: sorted(v) for k, v in dirty.items()}])
        )
    )

"""Observe operands of high-cardinality literal comparisons in parent and pool workers."""

import ast
import builtins
from collections import defaultdict
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import pickle
import sys
from multiprocessing.util import Finalize

active = "*"
records = defaultdict(lambda: defaultdict(set))
opaque = defaultdict(set)
bounds = defaultdict(dict)
configuration = None
reports = None
project = None


def location(node):
    return f"{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"


def observe(site, value):
    expected = configuration["types"][site]
    if expected == "range" and type(value) is int:
        limits = bounds[site].get(active)
        if limits is None:
            bounds[site][active] = [value, value]
        elif value < limits[0]:
            limits[0] = value
        elif value > limits[1]:
            limits[1] = value
    elif expected != "range" and type(value) is (str if expected == "str" else int):
        records[site][value].add(active)
    else:
        opaque[site].add(active)
    return value


def write_report():
    path = reports / (str(os.getpid()) + ".json")
    path.write_text(
        json.dumps(
            {
                "exact": {
                    site: [[value, sorted(owners)] for value, owners in values.items()]
                    for site, values in records.items()
                },
                "bounds": bounds,
                "opaque": {site: sorted(names) for site, names in opaque.items()},
            },
            separators=(",", ":"),
        )
    )


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = Path(self.path)
        ordered_sites = configuration["sources"][path.relative_to(project).as_posix()]
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            data = ordered_sites.get(location(node))
            if not data:
                continue
            site, side = data
            subject = node.left if side == "left" else node.comparators[0]
            wrapped = ast.copy_location(
                ast.Call(
                    ast.Name("_br_literal_operand_", ast.Load()),
                    [ast.Constant(site), subject],
                    [],
                ),
                subject,
            )
            if side == "left":
                node.left = wrapped
            else:
                node.comparators[0] = wrapped
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("sqlglot"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.origin and spec.origin.endswith(".py"):
            local = Path(spec.origin)
            if (
                local.is_relative_to(project)
                and local.relative_to(project).as_posix() in configuration["sources"]
            ):
                spec.loader = Loader(fullname, spec.origin)
        return spec


def start(folder):
    global configuration, reports, project
    if configuration is not None:
        return
    reports = Path(folder)
    configuration = json.loads((reports / "config").read_bytes())
    project = Path(configuration["root"])
    if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
        raise RuntimeError(
            "Project already imported before collecting baseline operands"
        )
    builtins._br_literal_operand_ = observe
    sys.meta_path.insert(0, Finder())
    (reports / (str(os.getpid()) + ".started")).touch()
    return Finalize(None, write_report, exitpriority=-1)


def task(owner, payload):
    global active
    before, active = active, owner
    try:
        fun, args, kwargs = pickle.loads(payload)
        return fun(*args, **kwargs)
    finally:
        active = before

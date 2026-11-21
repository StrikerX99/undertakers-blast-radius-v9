"""Fingerprint the optimized expression graph passed to SQLGlot's planner."""

import enum
import hashlib
import json
from functools import lru_cache


@lru_cache(maxsize=None)
def validate_slots(klass):
    supported = {
        "args",
        "parent",
        "arg_key",
        "index",
        "comments",
        "_type",
        "_meta",
        "_hash",
        "__dict__",
        "__weakref__",
    }
    for ancestor in klass.__mro__:
        if not supported.issuperset(ancestor.__dict__.get("__slots__", ())):
            raise ValueError("Unknown expression slots")


def expression_digest(root):
    from sqlglot import exp

    seen, nodes, objects, caches = {}, [], [], []

    def reference(v):
        kind = type(v)
        if kind in (str, bool, int, type(None)):
            return v
        if kind is float:
            return ["float", v.hex()]
        if isinstance(v, enum.Enum):
            return ["enum", kind.__module__, kind.__qualname__, v.name]
        key = id(v)
        if key not in seen:
            seen[key] = len(nodes)
            nodes.append(None)
            objects.append(v)
        return ["ref", seen[key]]

    reference(root)
    for i, v in enumerate(objects):
        if isinstance(v, exp.Expr):
            if getattr(v, "__dict__", None):
                raise ValueError("Unexpected dynamically attached expression state")
            validate_slots(type(v))
            nodes[i] = [
                type(v).__module__,
                type(v).__qualname__,
                reference(v.args),
                reference(v.parent),
                reference(v.arg_key),
                reference(v.index),
                reference(v.comments),
                reference(v._type),
                reference(v._meta),
                v._hash is not None,
            ]
            caches.append((v, v._hash))
        elif type(v) in (list, tuple):
            nodes[i] = [type(v).__name__, [reference(x) for x in v]]
        elif type(v) is dict:
            nodes[i] = ["dict", [[reference(k), reference(x)] for k, x in v.items()]]
        else:
            raise ValueError("Unsupported expression graph member")
    # Hashes include interpreter-specific salts. Check cache consistency, then
    # retain the presence of each cache in the portable representation above.
    try:
        for node, _ in caches:
            node._hash = None
        for node, old in caches:
            if old is not None and hash(node) != old:
                raise ValueError("Stale expression hash")
    finally:
        for node, old in caches:
            node._hash = old
    return hashlib.sha256(
        json.dumps(nodes, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()

import hashlib, json


def fingerprint(value):
    from sqlglot import exp

    seen = set()

    def walk(v, parent=None, key=None, idx=None):
        if isinstance(v, exp.Expr):
            if (
                id(v) in seen
                or v.parent is not parent
                or v.arg_key != key
                or v.index != idx
            ):
                raise ValueError
            if (
                v.comments is not None
                or v._type is not None
                or v._meta is not None
                or v._hash is not None
            ):
                raise ValueError
            if getattr(v, "__dict__", None):
                raise ValueError
            seen.add(id(v))
            return [
                "Expr",
                type(v).__module__,
                type(v).__qualname__,
                [[k, walk(a, v, k)] for k, a in v.args.items()],
            ]
        if type(v) is list:
            if id(v) in seen:
                raise ValueError
            seen.add(id(v))
            return ["list", [walk(x, parent, key, i) for i, x in enumerate(v)]]
        if type(v) not in (str, bool, int, type(None)):
            raise ValueError
        return [type(v).__name__, v]

    return hashlib.sha256(
        json.dumps(walk(value), separators=(",", ":")).encode()
    ).hexdigest()

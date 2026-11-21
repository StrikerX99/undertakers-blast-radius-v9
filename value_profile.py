"""Portable baseline operand observations; never contains mutation answers."""

import enum
import math
import operator

OPS = {
    "Eq": operator.eq,
    "NotEq": operator.ne,
    "Is": operator.is_,
    "IsNot": operator.is_not,
    "Lt": operator.lt,
    "LtE": operator.le,
    "Gt": operator.gt,
    "GtE": operator.ge,
    "In": lambda a, b: a in b,
    "NotIn": lambda a, b: a not in b,
}
UNKNOWN = ("unknown",)


def snapshot(value, depth=0):
    cls = type(value)
    if value is None:
        return ("none",)
    if cls in (str, int, bool, float):
        if cls is str and len(value) > 100:
            return UNKNOWN
        if cls is float and not math.isfinite(value):
            return UNKNOWN
        return (cls.__name__, value)
    if (
        isinstance(value, enum.Enum)
        and isinstance(value.value, (str, int))
        and cls.__eq__ in (object.__eq__, str.__eq__, int.__eq__)
        and cls.__ne__ in (object.__ne__, str.__ne__, int.__ne__)
    ):
        return (
            "enum",
            cls.__module__ + "." + cls.__qualname__,
            value.value,
            isinstance(value, str),
            isinstance(value, int),
        )
    if cls is tuple and depth < 2 and len(value) <= 8:
        parts = tuple(snapshot(v, depth + 1) for v in value)
        if UNKNOWN not in parts:
            return ("tuple", parts)
    if cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__:
        return ("identity", cls.__module__ + "." + cls.__qualname__)
    return UNKNOWN


def decode(value):
    kind = value[0]
    if kind == "none":
        return None
    if kind in ("str", "int", "bool", "float"):
        return value[1]
    if kind == "tuple":
        return tuple(decode(v) for v in value[1])
    raise ValueError("Unsupported value")


def compare(op, left, right, identical):
    """Evaluate only operations whose recorded builtin semantics are sufficient."""
    if op in ("Is", "IsNot"):
        return identical if op == "Is" else not identical
    if left[0] == "unknown" or right[0] == "unknown":
        raise ValueError("Unknown operands")
    if op in ("Eq", "NotEq"):
        if {left[0], right[0]} == {"none", "identity"}:
            result = False
        elif left[0] == right[0] == "identity":
            result = identical
        elif left[0] == "enum" and right[0] == "enum":
            if left[1] == right[1]:
                result = left[2] == right[2]
            elif left[3] and right[3] or left[4] and right[4]:
                result = left[2] == right[2]
            else:
                result = False
        elif left[0] == "enum" or right[0] == "enum":
            member, other = (left, right) if left[0] == "enum" else (right, left)
            if (
                member[3]
                and other[0] == "str"
                or member[4]
                and other[0] in ("int", "bool", "float")
            ):
                result = member[2] == other[1]
            elif other[0] == "none":
                result = False
            else:
                raise ValueError("Unknown enum comparison")
        else:
            result = decode(left) == decode(right)
        return result if op == "Eq" else not result
    return bool(OPS[op](decode(left), decode(right)))

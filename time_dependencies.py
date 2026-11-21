"""Cover all consumers of the date/time dictionaries, aliases and derived tries."""

import ast


def literal_entry(key, value):
    return (
        isinstance(key, ast.Constant)
        and type(key.value) in (str, int)
        and isinstance(value, ast.Constant)
        and type(value.value) is str
    )


class TimeDependencies:
    def __init__(self, data, by_name, external):
        if data["commit"] != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
            raise ValueError("Unrecognized time dependency map")
        self.locations = {
            path: {tuple(span) for span in spans}
            for path, spans in data["locations"].items()
        }

        def numbers(names):
            return {by_name[name] for name in names if name in by_name}

        self.readers = numbers(data["readers"])
        self.scoped = {p: numbers(ids) for p, ids in data.get("paths", {}).items()}
        self.external = external

    def readers_for(self, path, old, new, equal):
        if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
            return None
        span = old.lineno, old.col_offset, old.end_lineno, old.end_col_offset
        if span not in self.locations.get(path, ()):
            return None
        # Literal keys can be reordered, added, removed or collide. Keep
        # non-literal expressions (such as dictionary unpacking) intact.
        old_dynamic = [
            (k, v) for k, v in zip(old.keys, old.values) if not literal_entry(k, v)
        ]
        new_dynamic = [
            (k, v) for k, v in zip(new.keys, new.values) if not literal_entry(k, v)
        ]
        if len(old_dynamic) != len(new_dynamic) or any(
            not equal(a, c) or not equal(b, d)
            for (a, b), (c, d) in zip(old_dynamic, new_dynamic)
        ):
            return None
        return (
            self.scoped.get(path, self.readers)
            | self.external.external
            | self.external.external_by_source.get(path, set())
        )

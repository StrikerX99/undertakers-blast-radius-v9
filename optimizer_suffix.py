"""Skip a verified suffix of the pinned optimizer when its complete input is unchanged."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path
from checkpoint_graph import expression_digest


@lru_cache(maxsize=1)
def evidence():
    return json.loads(Path(__file__).with_name("optimizer-suffix.json").read_bytes())


class Suffix:
    def __init__(self, root, changed_sites, revision):
        from sqlglot.optimizer.optimizer import RULES

        self.rules = RULES
        self.sites = changed_sites
        profile = evidence()
        path = root / "sqlglot/optimizer/optimizer.py"
        verified = (
            profile["pin"] == revision
            and profile["optimizer"] == hashlib.sha256(path.read_bytes()).hexdigest()
            and profile["rules"] == [fn.__module__ + ":" + fn.__name__ for fn in RULES]
        )
        self.queries = profile["queries"] if verified else {}

    def options(self, index, matched):
        row = self.queries.get(str(index))
        if not row:
            return {}
        # Missing or unattributed calls receive the original complete replay.
        stop = max((row["last"].get(name, -1) for name in self.sites), default=-1)
        expected = row["graphs"].get(str(stop))
        if expected is None:
            return {}

        def compare(expression):
            try:
                digest = expression_digest(expression)
            except ValueError:
                return expression
            if digest == expected:
                raise matched
            return expression

        return {"rules": (*self.rules[: stop + 1], compare, *self.rules[stop + 1 :])}

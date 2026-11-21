"""Compute cardinality before the outer sort and limit from the pinned DuckDB fixture data."""

import hashlib
import json
from pathlib import Path
import sys


def sql_hash(sql):
    return hashlib.sha256(sql.encode()).hexdigest()


def pattern(sql, conn, parse_one, exp):
    node = parse_one(sql, read="duckdb")
    order = node.args.get("order")
    distinct = node.args.get("distinct")
    if (
        not order
        or not isinstance(node, exp.Select)
        or (distinct and distinct.args.get("on"))
    ):
        return None
    # Accept the generator's precise terminal clause. Embedded sorts and window
    # orders remain inside the unchanged literal prefix.
    keys = [o.sql("duckdb") for o in order.expressions]
    clause = "ORDER BY " + ", ".join(keys)
    suffix = "".join(
        " " + node.args[k].sql("duckdb")
        for k in ("limit", "offset")
        if node.args.get(k)
    )
    if not sql.endswith(" " + clause + suffix):
        return None
    before = sql[: -len(clause + suffix)]
    unsorted = before.strip()
    if (
        conn.execute("SELECT count(*) FROM (" + unsorted + ") AS br_input").fetchone()[
            0
        ]
        > 1
    ):
        return None
    # Only interleaved NULLS FIRST/LAST modifiers can vary. Everything else,
    # including key expressions, aliases, projection, inner sorts and limits,
    # must match the literal baseline bytes.
    parts = []
    for n, key in enumerate(keys):
        for tail in (" NULLS FIRST", " NULLS LAST"):
            if key.endswith(tail):
                key = key[: -len(tail)]
                break
        parts.append((before + "ORDER BY " if n == 0 else ", ") + key)
    parts.append(suffix)
    return parts


def main():
    root, app, output = [Path(s).resolve() for s in sys.argv[1:4]]
    sys.path.insert(0, str(root))
    import duckdb
    from sqlglot import exp, parse_one, transpile
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    fixture = root / "tests/fixtures/optimizer/tpc-ds"
    baseline = json.loads((app / "executor-queries.json").read_bytes())
    current = json.loads((app / "coverage-map.json").read_bytes())
    hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            root / "tests/helpers.py",
            root / "tests/test_executor.py",
            fixture / "tpc-ds.sql",
            *[fixture / (k + ".csv.gz") for k in TPCDS_SCHEMA],
        ]
    }
    if any(hashes[k] != current["source_hashes"].get(k) for k in hashes):
        raise ValueError("Pinned fixture mismatch")
    conn = duckdb.connect(config={"threads": "1"})
    for name, cols in TPCDS_SCHEMA.items():
        path = fixture / (name + ".csv.gz")
        conn.execute(
            f"CREATE VIEW {name} AS SELECT * FROM READ_CSV('{path}', delim='|', header=True, columns={cols})"
        )
    variants = {}
    for i, (args, raw, _) in enumerate(
        load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")
    ):
        if not args.get("execute"):
            continue
        sql = transpile(raw, write="duckdb")[0]
        ref = baseline["queries"][str(i)]
        if ref["sql"] != sql_hash(raw) or ref["duck_sql"] != sql_hash(sql):
            raise ValueError(("Unexpected SQL", i))
        parts = pattern(sql, conn, parse_one, exp)
        if parts:
            variants[str(i)] = parts
    output.write_text(
        json.dumps(
            dict(commit=baseline["commit"], files=hashes, patterns=variants),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    print("Queries with individually verified zero/one sort input", len(variants))


if __name__ == "__main__":
    main()

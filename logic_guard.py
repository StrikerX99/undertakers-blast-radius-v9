"""Check Boolean guard rewrites using per-test observations on the baseline.

A test can be removed only when all modeled executions retain the same truth
value and compatible Python evaluation order. A newly evaluated unknown atom,
or omission of a potentially effectful atom, keeps the test in the selection.
"""

import ast


def _formula(node, atoms):
    if isinstance(node, ast.BoolOp):
        op = "and" if isinstance(node.op, ast.And) else "or"
        return (op, [_formula(child, atoms) for child in node.values])
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return ("not", _formula(node.operand, atoms))
    if isinstance(node, ast.Constant) and type(node.value) in (
        bool,
        int,
        float,
        str,
        type(None),
    ):
        return ("const", bool(node.value))
    key = ast.dump(node)
    if key in atoms:
        # Two calls with the same syntax may have different effects or return
        # different results. Refuse to identify occurrences by their source text.
        raise ValueError("Repeated guard atom")
    atoms[key] = node
    return ("atom", key)


def _evaluate(formula, lookup):
    op, value = formula
    if op == "const":
        return value
    if op == "atom":
        return lookup(value)
    if op == "not":
        return not _evaluate(value, lookup)
    if op == "and":
        return all(_evaluate(child, lookup) for child in value)
    return any(_evaluate(child, lookup) for child in value)


def _values(selector, path, node, universe):
    """Return conservative true and false domains for a baseline comparison."""
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return universe, universe
    key = f"{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
    data = selector.mapping.get("comparisons", {}).get(path, {}).get(key)
    if data is None:
        return universe, universe
    positive, negative = set(), set()
    unknown = selector.sets[data["unknown"]] & universe
    for (_, _, _, before), slot in data["variants"]:
        tests = selector.sets[slot] & universe
        (positive if before else negative).update(tests)
    unobserved = universe - positive - negative
    uncertain = unknown | unobserved
    return positive | uncertain, negative | uncertain


def _variant_ok(new_formula, trace, before):
    cursor = 0
    omitted = []

    def lookup(key):
        nonlocal cursor
        while cursor < len(trace) and trace[cursor][0] != key:
            omitted.append(trace[cursor][0])
            cursor += 1
        if cursor == len(trace):
            # The changed guard evaluates an expression that did not run on
            # this baseline path, or requests a different evaluation order.
            raise KeyError(key)
        result = trace[cursor][1]
        cursor += 1
        return result

    try:
        after = _evaluate(new_formula, lookup)
    except KeyError:
        return None
    if before != after:
        return None
    omitted.extend(key for key, _ in trace[cursor:])
    return omitted


def analyze(selector, path, old, new, same):
    """Return affected IDs for a changed guard, or None for an unsupported edit."""
    if type(old) is not type(new) or not isinstance(
        old, (ast.If, ast.While, ast.IfExp)
    ):
        return None
    before, after = old.test, new.test
    if not isinstance(before, (ast.BoolOp, ast.UnaryOp)) and not isinstance(
        after, (ast.BoolOp, ast.UnaryOp)
    ):
        return None
    if (
        same(before, after)
        or not same(old.body, new.body)
        or not same(old.orelse, new.orelse)
    ):
        return None
    # Adding or removing assignment targets, yield points or await changes
    # function compilation even if the guard is never reached.
    if any(
        (
            isinstance(node, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await))
            or (isinstance(node, ast.Name) and node.id in ("super", "__class__"))
        )
        for expr in (before, after)
        for node in ast.walk(expr)
    ):
        return None
    old_atoms, new_atoms = {}, {}
    try:
        old_formula, new_formula = (
            _formula(before, old_atoms),
            _formula(after, new_atoms),
        )
    except ValueError:
        return None
    if len(old_atoms) > 7 or len(new_atoms) > 8:
        return None
    if not set(old_atoms) & set(new_atoms):
        return None
    universe = selector.line_tests(path, range(before.lineno, before.end_lineno + 1))
    if not universe:
        # Lack of a trace may reflect collection or an untracked concurrent path.
        return None
    domains = {
        key: _values(selector, path, node, universe) for key, node in old_atoms.items()
    }
    pending = [((), universe)]
    affected = set()
    while pending:
        trace, candidates = pending.pop()
        previous = dict(trace)
        # Stop at the first not-yet-bound atom; use its baseline observations
        # to branch. Subsequent atoms may still short-circuit.
        try:
            result = _evaluate(old_formula, previous.__getitem__)
        except KeyError as exc:
            key = exc.args[0]
            for value, possible in zip((True, False), domains[key][:2]):
                subset = candidates & possible
                if subset:
                    pending.append((trace + ((key, value),), subset))
            continue
        omissions = _variant_ok(new_formula, trace, result)
        if omissions is None or omissions:
            # A successful value profile cannot prove the atom never raised an
            # exception in another invocation. Preserve every actually evaluated
            # nonconstant atom, including its effect/order and exception behavior.
            affected.update(candidates)
        if affected == universe:
            break
    return None if affected == universe else affected

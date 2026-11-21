"""Evaluate worker counts in integer milliseconds with deterministic load balancing."""

from heapq import heapreplace


def exact_two(jobs, total, incumbent):
    """Find a strictly shorter two-worker makespan, subject to a memory cap.

    Bit s denotes that some subset of the prefix consumes s milliseconds.
    Keep prefix bitsets for deterministic reconstruction only up to half of
    the total. A larger subset merely exchanges the two workers.
    """
    target = total // 2
    if target == 0 or target > 65_536 or len(jobs) * (target + 1) > 32_000_000:
        return None
    mask = (1 << (target + 1)) - 1
    reachable = 1
    history = []
    for duration, number in jobs:
        if not 0 < duration <= target:
            continue
        previous = reachable
        reachable |= (reachable << duration) & mask
        if previous != reachable:
            history.append((duration, number, previous))
            if reachable.bit_length() == target + 1:
                break
    lighter = reachable.bit_length() - 1
    heavier = total - lighter
    if heavier >= incumbent or lighter == 0:
        return None
    budget = lighter
    owned = set()
    for duration, number, previous in reversed(history):
        if not (previous >> budget) & 1:
            owned.add(number)
            budget -= duration
        if budget == 0:
            break
    workers = [[], []]
    for _, number in jobs:
        workers[0 if number in owned else 1].append(number)
    return workers, heavier


def schedule(tests):
    if not tests:
        return [], 0
    jobs = sorted(
        ((round(test["seconds"] * 1000), test["number"]) for test in tests),
        key=lambda job: (-job[0], job[1]),
    )
    durations = {number: milliseconds for milliseconds, number in jobs}
    total = sum(durations.values())
    longest = jobs[0][0]
    # When zero-duration tests exist, the v2 longest-first allocator can put
    # every one on the same worker. No greater worker count could improve a
    # singleton load, and empty worker slots do not incur the 2s charge.
    capacity = max(1, sum(milliseconds > 0 for milliseconds, _ in jobs))
    best = None
    for count in range(1, min(8, capacity) + 1):
        optimistic = max(longest, (total + count - 1) // count) + 2000 * count
        if best is not None and optimistic >= best[0]:
            continue
        workers = [[] for _ in range(count)]
        heap = [(0, worker) for worker in range(count)]
        for seconds, number in jobs:
            load, worker = heap[0]
            workers[worker].append(number)
            heapreplace(heap, (load + seconds, worker))
        loads = [0] * count
        for load, worker in heap:
            loads[worker] = load
        for _ in range(16):
            high = max(range(count), key=loads.__getitem__)
            low = min(range(count), key=loads.__getitem__)
            gap = loads[high] - loads[low]
            if high == low or len(workers[high]) <= 1:
                break
            movable = (n for n in workers[high] if 0 < durations[n] < gap)
            n = min(movable, key=lambda n: abs(gap - 2 * durations[n]), default=None)
            if n is None:
                break
            workers[high].remove(n)
            workers[low].append(n)
            loads[high] -= durations[n]
            loads[low] += durations[n]
        makespan = max(loads)
        cost = makespan + 2000 * count
        if count == 2 and optimistic < cost:
            # An earlier one-worker score can be the tighter incumbent.
            threshold = min(cost, best[0]) - 2000 * count
            improved = exact_two(jobs, total, threshold)
            if improved is not None:
                workers, makespan = improved
                cost = makespan + 2000 * count
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1], best[0]


def fill_spare_capacity(workers, selected, plausible, durations, zero_cost):
    """Keep extra tests when they fit within the already-paid worker makespan."""
    if not workers:
        return 0
    free_tests = sorted(zero_cost - selected)
    workers[0].extend(free_tests)
    added = len(free_tests)
    if len(workers) == 1 or not plausible:
        return added
    loads = [sum(durations[n] for n in worker) for worker in workers]
    ceiling = max(loads)
    space = [ceiling - load for load in loads]
    remaining = plausible - selected - zero_cost
    for n in sorted(remaining, key=lambda n: (durations[n], n)):
        worker = max(range(len(space)), key=space.__getitem__)
        duration = durations[n]
        if duration > space[worker]:
            break
        workers[worker].append(n)
        space[worker] -= duration
        added += 1
    return added

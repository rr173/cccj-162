"""Demonstration suite for the row-sequence query operator.

Run via:  python3 -m rowseq.cli demo
Every demonstration asserts its claims; a failure raises AssertionError.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import tracemalloc

from .api import run_query, cross_check, QueryError
from .pattern import parse_pattern, validate_pattern, PatternError
from .engine import StepLimitError
from . import exhaustive


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE events ("
              "  id INTEGER, pk TEXT, ts INTEGER, kind TEXT, v INTEGER)")
    return c


def _insert(conn, rows):
    conn.executemany("INSERT INTO events (id, pk, ts, kind, v) VALUES (?,?,?,?,?)", rows)


def _eq_kind(k):
    return {"cmp": {"op": "eq", "left": {"field": "kind"}, "right": {"lit": k}}}


def _var(name, cond=None):
    d = {"var": name}
    if cond is not None:
        d["cond"] = cond
    return d


def _fmt_caps(caps):
    return " ".join(f"{v}={ids}" for v, ids in sorted(caps.items()))


def _show(out, m):
    meas = " ".join(
        f"{v}(count={mm['count']},first={mm['first_row_id']},last={mm['last_row_id']})"
        for v, mm in sorted(m.measures.items()))
    out(f"    match pk={m.partition} start={m.start_row_id} end={m.end_row_id} "
        f"next_start={m.next_start_row_id}")
    out(f"      captures: {_fmt_caps(m.captures)}")
    out(f"      measures: {meas}")


def _ok(out, msg):
    out(f"  OK  {msg}")


# ---------------------------------------------------------------------------
# D1: A B* C -- greedy repetition must backtrack to leave the final C
# ---------------------------------------------------------------------------

def demo_d1(out):
    out("\n" + "=" * 72)
    out("D1  A B* C: greedy B* backtracks so the final C remains for C")
    out("=" * 72)
    conn = _conn()
    _insert(conn, [(1, "p", 1, "A", None), (2, "p", 2, "B", 20),
                   (3, "p", 3, "B", 30), (4, "p", 4, "C", 40)])
    # B's condition ALSO accepts the C row: greedy B* first swallows
    # rows 2,3,4, then C has nothing left -> backtrack -> B* keeps 2,3.
    b_cond = {"or": [_eq_kind("B"), _eq_kind("C")]}
    pattern = {"seq": [_var("A", _eq_kind("A")),
                       {"rep": {"of": _var("B", b_cond), "min": 0, "max": None,
                                "mode": "greedy"}},
                       _var("C", _eq_kind("C"))]}
    res = run_query(conn, table="events", pattern=pattern,
                    partition_key="pk", order_value="ts", row_id="id")
    out(f"  rows: 1:A 2:B 3:B 4:C   (the C row also satisfies B's condition)")
    for m in res.matches:
        _show(out, m)
    assert len(res.matches) == 1
    m = res.matches[0]
    assert m.captures["B"] == [2, 3], m.captures
    assert m.start_row_id == 1 and m.end_row_id == 4
    _ok(out, "greedy B* swallowed 2,3,4, failed C, backtracked, kept B=[2,3], C=4")


# ---------------------------------------------------------------------------
# D2: (A B)|(A C) -- left branch wins only on COMPLETE success
# ---------------------------------------------------------------------------

def demo_d2(out):
    out("\n" + "=" * 72)
    out("D2  (A B)|(A C): left-branch priority holds only for complete matches")
    out("=" * 72)
    conn = _conn()
    _insert(conn, [(1, "pa", 1, "A", None), (2, "pa", 2, "C", None),
                   (3, "pb", 1, "A", None), (4, "pb", 2, "B", None)])
    pattern = {"alt": [{"seq": [_var("A", _eq_kind("A")), _var("B", _eq_kind("B"))]},
                       {"seq": [_var("A", _eq_kind("A")), _var("C", _eq_kind("C"))]}]}
    res = run_query(conn, table="events", pattern=pattern,
                    partition_key="pk", order_value="ts", row_id="id")
    for m in res.matches:
        _show(out, m)
    assert len(res.matches) == 2
    m1, m2 = res.matches
    # pa: left branch A B fails at row 2 -> right branch A C completes
    assert m1.partition == "pa" and m1.captures == {"A": [1], "B": [], "C": [2]}, m1.captures
    # pb: left branch completes -> it wins
    assert m2.partition == "pb" and m2.captures == {"A": [3], "B": [4], "C": []}, m2.captures
    _ok(out, "pa: left branch failed mid-way -> right branch won; "
             "pb: left branch completed -> left branch won")


# ---------------------------------------------------------------------------
# D3: two histories reach the same (state,row) with different LAST(B);
#     only one can complete -> the engine must NOT merge by (state,row)
# ---------------------------------------------------------------------------

def demo_d3(out):
    out("\n" + "=" * 72)
    out("D3  no over-merge: same (state,row), different LAST(B) histories")
    out("=" * 72)
    conn = _conn()
    _insert(conn, [(1, "p", 1, "BX", 1), (2, "p", 2, "BX", 2), (3, "p", 3, "C", 3)])
    bx = _eq_kind("BX")
    c_cond = {"and": [_eq_kind("C"),
                      {"cmp": {"op": "eq",
                               "left": {"last": {"var": "B", "field": "v"}},
                               "right": {"lit": 2}}}]}
    pattern = {"seq": [{"alt": [{"seq": [_var("B", bx), _var("X", bx)]},
                                {"seq": [_var("X", bx), _var("B", bx)]}]},
                       _var("C", c_cond)]}
    out("  rows: 1:BX(v=1) 2:BX(v=2) 3:C ;  C requires LAST(B).v == 2")
    out("  path 1 (B then X): B=[1] -> LAST(B).v=1 -> C fails")
    out("  path 2 (X then B): B=[2] -> LAST(B).v=2 -> C succeeds")
    res = run_query(conn, table="events", pattern=pattern,
                    partition_key="pk", order_value="ts", row_id="id")
    for m in res.matches:
        _show(out, m)
    assert len(res.matches) == 1
    assert res.matches[0].captures["B"] == [2] and res.matches[0].captures["X"] == [1]
    _ok(out, "engine explored the second history and found the match B=[2],X=[1]")

    # diagnostic: a naive (pc,row) merge would prune the second history
    naive = run_query(conn, table="events", pattern=pattern,
                      partition_key="pk", order_value="ts", row_id="id",
                      naive_state_merge=True)
    out(f"  diagnostic: naive (state,row)-only merge -> {len(naive.matches)} matches "
        "(WRONG: the second history was discarded)")
    assert len(naive.matches) == 0
    _ok(out, "sound keys include live capture history; naive merge demonstrably loses matches")


# ---------------------------------------------------------------------------
# D4: reluctant vs greedy produce different captures
# ---------------------------------------------------------------------------

def demo_d4(out):
    out("\n" + "=" * 72)
    out("D4  reluctant vs greedy: different captures for A B*")
    out("=" * 72)
    conn = _conn()
    _insert(conn, [(1, "p", 1, "A", None), (2, "p", 2, "B", None), (3, "p", 3, "B", None)])
    for mode in ("greedy", "reluctant"):
        pattern = {"seq": [_var("A", _eq_kind("A")),
                           {"rep": {"of": _var("B", _eq_kind("B")), "min": 0,
                                    "max": None, "mode": mode}}]}
        res = run_query(conn, table="events", pattern=pattern,
                        partition_key="pk", order_value="ts", row_id="id")
        m = res.matches[0]
        out(f"  {mode:9s}: ", )
        _show(out, m)
        if mode == "greedy":
            assert m.captures["B"] == [2, 3] and m.end_row_id == 3
        else:
            assert m.captures["B"] == [] and m.end_row_id == 1
            assert m.measures["B"]["count"] == 0
    _ok(out, "greedy captured B=[2,3]; reluctant exited immediately, B=[] (match is just A)")


# ---------------------------------------------------------------------------
# D5: overlapping matches with next_row
# ---------------------------------------------------------------------------

def demo_d5(out):
    out("\n" + "=" * 72)
    out("D5  overlapping matches: X X over rows X,X,X with next_row vs past_last")
    out("=" * 72)
    conn = _conn()
    _insert(conn, [(1, "p", 1, "X", None), (2, "p", 2, "X", None), (3, "p", 3, "X", None)])
    pattern = {"seq": [_var("X", _eq_kind("X")), _var("X", _eq_kind("X"))]}
    res_next = run_query(conn, table="events", pattern=pattern,
                         partition_key="pk", order_value="ts", row_id="id",
                         skip="next_row")
    res_past = run_query(conn, table="events", pattern=pattern,
                         partition_key="pk", order_value="ts", row_id="id",
                         skip="past_last")
    out("  skip=next_row:")
    for m in res_next.matches:
        _show(out, m)
    out("  skip=past_last:")
    for m in res_past.matches:
        _show(out, m)
    assert [(m.start_row_id, m.end_row_id) for m in res_next.matches] == [(1, 2), (2, 3)]
    assert [(m.start_row_id, m.end_row_id) for m in res_past.matches] == [(1, 2)]
    assert res_next.matches[0].captures["X"] == [1, 2]  # same var twice: merged in order
    _ok(out, "next_row produced overlapping matches (1,2) and (2,3); "
             "same variable in two positions merged captures in consumption order")


# ---------------------------------------------------------------------------
# D6: to_first skip -- success and the two failure modes
# ---------------------------------------------------------------------------

def demo_d6(out):
    out("\n" + "=" * 72)
    out("D6  skip to_first(variable): advancing, and failure with explanation")
    out("=" * 72)
    conn = _conn()
    _insert(conn, [(1, "p", 1, "A", None), (2, "p", 2, "B", None),
                   (3, "p", 3, "C", None), (4, "p", 4, "A", None),
                   (5, "p", 5, "B", None), (6, "p", 6, "C", None)])
    pattern = {"seq": [_var("A", _eq_kind("A")),
                       {"rep": {"of": _var("B", _eq_kind("B")), "min": 0, "max": None}},
                       _var("C", _eq_kind("C"))]}
    res = run_query(conn, table="events", pattern=pattern,
                    partition_key="pk", order_value="ts", row_id="id",
                    skip="to_first:C")
    out("  A B* C with skip to_first:C over A,B,C,A,B,C:")
    for m in res.matches:
        _show(out, m)
    assert [(m.start_row_id, m.end_row_id, m.next_start_row_id) for m in res.matches] \
        == [(1, 3, 3), (4, 6, 6)]
    _ok(out, "to_first(C) moved the next start to the C rows 3 and 6")

    # failure 1: variable never captured
    conn2 = _conn()
    _insert(conn2, [(1, "p", 1, "A", None), (2, "p", 2, "B", None)])
    ab = {"seq": [_var("A", _eq_kind("A")), _var("B", _eq_kind("B"))]}
    try:
        run_query(conn2, table="events", pattern=ab,
                  partition_key="pk", order_value="ts", row_id="id",
                  skip="to_first:Q")
        raise AssertionError("expected QueryError")
    except QueryError as e:
        out(f"  to_first(Q) -> query failed as required: {e}")
        _ok(out, "uncaptured variable -> explained failure, no silent strategy change")

    # failure 2: target does not advance past the current start
    try:
        run_query(conn2, table="events", pattern=ab,
                  partition_key="pk", order_value="ts", row_id="id",
                  skip="to_first:A")
        raise AssertionError("expected QueryError")
    except QueryError as e:
        out(f"  to_first(A) -> query failed as required: {e}")
        _ok(out, "target == current start -> explained failure, no infinite loop")


# ---------------------------------------------------------------------------
# D7: empty-match configurations are rejected
# ---------------------------------------------------------------------------

def demo_d7(out):
    out("\n" + "=" * 72)
    out("D7  configuration rejection: nullable pattern / nullable repeated body")
    out("=" * 72)
    cases = [
        ("whole pattern nullable (A*)",
         {"rep": {"of": _var("A", _eq_kind("A")), "min": 0, "max": None}}),
        ("repeated body nullable ((A|)<1..>)",
         {"seq": [_var("S", _eq_kind("S")),
                  {"rep": {"of": {"alt": [_var("A", _eq_kind("A")), {"seq": []}]},
                           "min": 1, "max": None}}]}),
    ]
    for label, spec in cases:
        try:
            validate_pattern(parse_pattern(spec))
            raise AssertionError(f"expected PatternError for {label}")
        except PatternError as e:
            out(f"  rejected [{label}]: {e}")
            _ok(out, "configuration refused before execution")


# ---------------------------------------------------------------------------
# D8: null semantics -- future variables, self COUNT, null fields
# ---------------------------------------------------------------------------

def demo_d8(out):
    out("\n" + "=" * 72)
    out("D8  null conditions: future variable, self-reference COUNT, NULL fields")
    out("=" * 72)
    conn = _conn()
    _insert(conn, [(1, "p", 1, "A", 10), (2, "p", 2, "B", 20), (3, "p", 3, "C", 30)])
    # (a) B's condition reads LAST(C).v before C is captured -> null
    b_future = {"cmp": {"op": "gt",
                        "left": {"last": {"var": "C", "field": "v"}},
                        "right": {"lit": 15}}}
    pattern = {"seq": [_var("A", _eq_kind("A")), _var("B", b_future), _var("C", _eq_kind("C"))]}
    res = run_query(conn, table="events", pattern=pattern,
                    partition_key="pk", order_value="ts", row_id="id")
    out(f"  (a) B cond LAST(C).v > 15 with C not yet captured -> matches: {len(res.matches)}")
    assert len(res.matches) == 0
    b_isnull = {"is_null": {"last": {"var": "C", "field": "v"}}}
    pattern2 = {"seq": [_var("A", _eq_kind("A")), _var("B", b_isnull), _var("C", _eq_kind("C"))]}
    res2 = run_query(conn, table="events", pattern=pattern2,
                     partition_key="pk", order_value="ts", row_id="id")
    out(f"      same with IS_NULL(LAST(C).v) -> matches: {len(res2.matches)}")
    assert len(res2.matches) == 1
    _ok(out, "referencing a future variable yields null; only IS_NULL passes")

    # (b) self COUNT gate: B allowed while COUNT(B) < 2 (null before first capture)
    conn3 = _conn()
    _insert(conn3, [(1, "p", 1, "B", None), (2, "p", 2, "B", None), (3, "p", 3, "B", None)])
    b_self = {"or": [{"is_null": {"count": {"var": "B"}}},
                     {"cmp": {"op": "lt",
                              "left": {"count": {"var": "B"}},
                              "right": {"lit": 2}}}]}
    pattern3 = {"rep": {"of": _var("B", b_self), "min": 1, "max": None}}
    res3 = run_query(conn3, table="events", pattern=pattern3,
                     partition_key="pk", order_value="ts", row_id="id")
    for m in res3.matches:
        _show(out, m)
    assert [m.captures["B"] for m in res3.matches] == [[1, 2], [3]]
    _ok(out, "self COUNT(B) sees previous captures only; B* stopped at 2 per match")

    # (c) NULL field value: equality is UNKNOWN, IS_NULL passes
    conn4 = _conn()
    _insert(conn4, [(1, "p", 1, "A", None)])
    pat_eq = _var("A", {"cmp": {"op": "eq", "left": {"field": "v"}, "right": {"lit": 5}}})
    pat_null = _var("A", {"is_null": {"field": "v"}})
    r_eq = run_query(conn4, table="events", pattern=pat_eq,
                     partition_key="pk", order_value="ts", row_id="id")
    r_null = run_query(conn4, table="events", pattern=pat_null,
                       partition_key="pk", order_value="ts", row_id="id")
    out(f"  (c) row v=NULL: v = 5 -> matches {len(r_eq.matches)}; "
        f"IS_NULL(v) -> matches {len(r_null.matches)}")
    assert len(r_eq.matches) == 0 and len(r_null.matches) == 1
    _ok(out, "comparison with NULL is UNKNOWN (does not pass); IS_NULL detects it")


# ---------------------------------------------------------------------------
# D9: partitions and same-timestamp ordering by (order_value, row_id)
# ---------------------------------------------------------------------------

def demo_d9(out):
    out("\n" + "=" * 72)
    out("D9  partition isolation and (ts,id) total order with equal timestamps")
    out("=" * 72)
    conn = _conn()
    _insert(conn, [(1, "p1", 10, "A", None), (3, "p1", 10, "B", None),
                   (2, "p2", 10, "A", None), (4, "p2", 10, "B", None),
                   (5, "p3", 7, "B", None), (6, "p3", 7, "A", None)])
    pattern = {"seq": [_var("A", _eq_kind("A")), _var("B", _eq_kind("B"))]}
    res = run_query(conn, table="events", pattern=pattern,
                    partition_key="pk", order_value="ts", row_id="id")
    for m in res.matches:
        _show(out, m)
    got = {(m.partition, m.start_row_id, m.end_row_id) for m in res.matches}
    assert got == {("p1", 1, 3), ("p2", 2, 4)}, got
    _ok(out, "same ts=10 ordered by id inside each partition; p1/p2 matched "
             "independently; p3 has B(id=5) before A(id=6) at ts=7 -> no match")


# ---------------------------------------------------------------------------
# D10: exhaustive interpreter cross-check on a small table
# ---------------------------------------------------------------------------

def demo_d10(out):
    out("\n" + "=" * 72)
    out("D10  cross-check: engine vs independent exhaustive interpreter")
    out("=" * 72)
    conn = _conn()
    _insert(conn, [(1, "p", 1, "A", 1), (2, "p", 2, "B", 2), (3, "p", 3, "B", 3),
                   (4, "p", 4, "C", 4), (5, "p", 5, "A", 5), (6, "p", 6, "C", 6),
                   (7, "p", 7, "D", 7)])
    pattern = {"seq": [{"alt": [{"seq": [_var("A", _eq_kind("A")),
                                         {"rep": {"of": _var("B", _eq_kind("B")),
                                                  "min": 1, "max": None,
                                                  "mode": "greedy"}}]},
                                {"seq": [_var("A", _eq_kind("A")),
                                         _var("C", _eq_kind("C"))]}]},
                       {"rep": {"of": _var("D", _eq_kind("D")), "min": 0, "max": 1,
                                "mode": "reluctant"}}]}
    ast = parse_pattern(pattern)
    rows = [dict(zip(("id", "pk", "ts", "kind", "v"), r))
            for r in conn.execute("SELECT * FROM events ORDER BY pk, ts, id")]

    out("  all complete paths from row 1, in public priority order:")
    for k, (s, e, caps) in enumerate(exhaustive.all_matches(ast, rows, 0), 1):
        cap = " ".join(f"{v}={[rows[i]['id'] for i in ix]}" for v, ix in sorted(caps.items()))
        out(f"    path {k}: rows {rows[s]['id']}..{rows[e-1]['id']}  {cap}")

    for skip in ("past_last", "next_row"):
        rep = cross_check(conn, table="events", pattern=pattern,
                          partition_key="pk", order_value="ts", row_id="id", skip=skip)
        n_ok = sum(1 for c in rep["checks"] if c["ok"])
        out(f"  skip={skip}: {n_ok}/{len(rep['checks'])} checks passed "
            f"(per-start first-path equality + whole-stream equality incl. next starts)")
        assert rep["ok"], json.dumps(rep["mismatches"], indent=2, default=str)
    _ok(out, "engine's first path per start == exhaustive head; match streams and "
             "next-start positions identical")


# ---------------------------------------------------------------------------
# D11: scale -- long repetition, late-failing suffix, pathological alternation
# ---------------------------------------------------------------------------

def demo_d11(out):
    out("\n" + "=" * 72)
    out("D11  scale: long repetition, late failure, memoized candidate states")
    out("=" * 72)
    n_b = 20000
    # (a) A, B x N, C -> one match, B captures N rows; no recursion involved
    conn = _conn()
    _insert(conn, [(1, "p", 0, "A", None)]
            + [(i + 2, "p", i + 1, "B", None) for i in range(n_b)]
            + [(n_b + 2, "p", n_b + 1, "C", None)])
    pattern = {"seq": [_var("A", _eq_kind("A")),
                       {"rep": {"of": _var("B", _eq_kind("B")), "min": 0, "max": None}},
                       _var("C", _eq_kind("C"))]}
    tracemalloc.start()
    t0 = time.perf_counter()
    res = run_query(conn, table="events", pattern=pattern,
                    partition_key="pk", order_value="ts", row_id="id",
                    include_rows=False)
    dt = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    m = res.matches[0]
    out(f"  (a) A B*{n_b} C: matches={len(res.matches)} B count="
        f"{m.measures['B']['count']} steps={res.stats['steps']} "
        f"max_choice_points={res.stats['max_choice_points']} "
        f"peak_mem={peak/1e6:.1f}MB time={dt:.2f}s (recursion limit "
        f"{sys.getrecursionlimit()} untouched: engine is iterative)")
    assert len(res.matches) == 1 and m.measures["B"]["count"] == n_b

    # (b) same without the final C: every start fails; work stays linear
    conn2 = _conn()
    _insert(conn2, [(1, "p", 0, "A", None)]
            + [(i + 2, "p", i + 1, "B", None) for i in range(n_b)])
    t0 = time.perf_counter()
    res2 = run_query(conn2, table="events", pattern=pattern,
                     partition_key="pk", order_value="ts", row_id="id",
                     include_rows=False)
    dt = time.perf_counter() - t0
    out(f"  (b) late-failing suffix (no C): matches={len(res2.matches)} "
        f"steps={res2.stats['steps']} backtracks={res2.stats['backtracks']} "
        f"time={dt:.2f}s")
    assert len(res2.matches) == 0

    # (c) pathological: (B|X)* Z over k rows matching both branches, no Z.
    #     Without state sharing the search is exponential; the sound memo
    #     (no condition reads captures here -> key = (pc,row)) collapses it.
    k = 24
    conn3 = _conn()
    _insert(conn3, [(i + 1, "p", i, "BX", None) for i in range(k)])
    bx = _eq_kind("BX")
    pat3 = {"seq": [{"rep": {"of": {"alt": [_var("B", bx), _var("X", bx)]},
                             "min": 0, "max": None}},
                    _var("Z", _eq_kind("Z"))]}
    try:
        run_query(conn3, table="events", pattern=pat3,
                  partition_key="pk", order_value="ts", row_id="id",
                  use_memo=False, max_steps=200_000, include_rows=False)
        raise AssertionError("expected StepLimitError")
    except StepLimitError as e:
        out(f"  (c) (B|X)* Z, k={k}, memo OFF: exploded as expected ({e})")
    res3 = run_query(conn3, table="events", pattern=pat3,
                     partition_key="pk", order_value="ts", row_id="id",
                     use_memo=True, include_rows=False)
    out(f"      same with sound memo: matches={len(res3.matches)} "
        f"steps={res3.stats['steps']} memo_states={res3.stats['memo_states']} "
        f"memo_hits={res3.stats['memo_hits']}")
    assert len(res3.matches) == 0
    assert res3.stats["steps"] < 100_000
    _ok(out, "no pre-expansion of paths; failed-state sharing keeps the "
             "pathological case linear; unbounded repetition never recurses")


# ---------------------------------------------------------------------------

def run_all_demos(out=print):
    out("rowseq demonstration suite")
    demo_d1(out)
    demo_d2(out)
    demo_d3(out)
    demo_d4(out)
    demo_d5(out)
    demo_d6(out)
    demo_d7(out)
    demo_d8(out)
    demo_d9(out)
    demo_d10(out)
    demo_d11(out)
    out("\n" + "=" * 72)
    out("ALL DEMONSTRATIONS PASSED")
    out("=" * 72)

"""Unit tests and randomized cross-checks for the rowseq operator.

Run:  python3 -m unittest discover -s tests -v   (from /workspace)
"""

import random
import sqlite3
import sys
import unittest

sys.path.insert(0, __file__.rsplit("/", 2)[0])  # make `rowseq` importable

from rowseq import (run_query, cross_check, QueryError, PatternError,
                    parse_pattern, validate_pattern, compile_pattern,
                    compute_live_vars, run_from, exhaustive, eval_cond)
from rowseq.conditions import compare


def make_conn(rows):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE events (id INTEGER, pk TEXT, ts INTEGER, kind TEXT, v INTEGER)")
    c.executemany("INSERT INTO events VALUES (?,?,?,?,?)", rows)
    return c


def eq_kind(k):
    return {"cmp": {"op": "eq", "left": {"field": "kind"}, "right": {"lit": k}}}


def var(name, cond=None):
    d = {"var": name}
    if cond is not None:
        d["cond"] = cond
    return d


Q = dict(table="events", partition_key="pk", order_value="ts", row_id="id")


# ---------------------------------------------------------------------------
# condition semantics
# ---------------------------------------------------------------------------

class TestConditions(unittest.TestCase):
    def test_same_type_comparison(self):
        self.assertIs(compare("eq", 1, 1), True)
        self.assertIs(compare("lt", 1, 2), True)
        self.assertIs(compare("gt", "b", "a"), True)
        self.assertIs(compare("eq", True, True), True)
        self.assertIs(compare("eq", 1, True), None)      # int64 vs bool -> unknown
        self.assertIs(compare("eq", "1", 1), None)       # text vs int64 -> unknown
        self.assertIs(compare("lt", True, False), None)  # no ordering on bool
        self.assertIs(compare("eq", None, None), None)   # null operand -> unknown
        self.assertIs(compare("ne", 1.5, 1.5), None)     # unsupported type

    def test_three_valued_logic(self):
        row = {"v": None}
        unknown = {"cmp": {"op": "eq", "left": {"field": "v"}, "right": {"lit": 1}}}
        self.assertIs(eval_cond(unknown, row, {}, [row]), None)
        self.assertIs(eval_cond({"not": unknown}, row, {}, [row]), None)
        self.assertIs(eval_cond({"and": [unknown, {"const": True}]}, row, {}, [row]), None)
        self.assertIs(eval_cond({"and": [unknown, {"const": False}]}, row, {}, [row]), False)
        self.assertIs(eval_cond({"or": [unknown, {"const": True}]}, row, {}, [row]), True)
        self.assertIs(eval_cond({"or": [unknown, {"const": False}]}, row, {}, [row]), None)

    def test_count_of_uncaptured_is_null(self):
        row = {"v": 1}
        cond = {"is_null": {"count": {"var": "B"}}}
        self.assertIs(eval_cond(cond, row, {}, [row]), True)
        self.assertIs(eval_cond(cond, row, {"B": [0]}, [row]), False)


# ---------------------------------------------------------------------------
# pattern validation
# ---------------------------------------------------------------------------

class TestValidation(unittest.TestCase):
    def test_nullable_root_rejected(self):
        with self.assertRaises(PatternError):
            validate_pattern(parse_pattern(
                {"rep": {"of": var("A"), "min": 0, "max": None}}))
        with self.assertRaises(PatternError):
            validate_pattern(parse_pattern({"seq": []}))
        with self.assertRaises(PatternError):
            validate_pattern(parse_pattern(
                {"alt": [var("A"), {"seq": []}]}))

    def test_nullable_repeated_body_rejected(self):
        with self.assertRaises(PatternError):
            validate_pattern(parse_pattern(
                {"seq": [var("S"),
                         {"rep": {"of": {"alt": [var("A"), {"seq": []}]},
                                  "min": 1, "max": None}}]}))

    def test_empty_branch_allowed_elsewhere(self):
        p = parse_pattern({"seq": [var("A"), {"alt": [var("B"), {"seq": []}]}]})
        validate_pattern(p)  # must not raise

    def test_bad_repetition_bounds(self):
        with self.assertRaises(PatternError):
            parse_pattern({"rep": {"of": var("A"), "min": 3, "max": 2}})
        with self.assertRaises(PatternError):
            parse_pattern({"rep": {"of": var("A"), "min": -1}})
        with self.assertRaises(PatternError):
            parse_pattern({"rep": {"of": var("A"), "mode": "eager"}})


# ---------------------------------------------------------------------------
# end-to-end behaviour (mirrors the demonstration suite)
# ---------------------------------------------------------------------------

class TestEndToEnd(unittest.TestCase):
    def test_d1_greedy_backtrack(self):
        conn = make_conn([(1, "p", 1, "A", None), (2, "p", 2, "B", 20),
                          (3, "p", 3, "B", 30), (4, "p", 4, "C", 40)])
        b = {"or": [eq_kind("B"), eq_kind("C")]}
        pat = {"seq": [var("A", eq_kind("A")),
                       {"rep": {"of": var("B", b), "min": 0, "max": None}},
                       var("C", eq_kind("C"))]}
        res = run_query(conn, pattern=pat, **Q)
        self.assertEqual(len(res.matches), 1)
        self.assertEqual(res.matches[0].captures["B"], [2, 3])
        self.assertEqual(res.matches[0].end_row_id, 4)

    def test_d2_left_branch_only_on_complete_success(self):
        conn = make_conn([(1, "pa", 1, "A", None), (2, "pa", 2, "C", None),
                          (3, "pb", 1, "A", None), (4, "pb", 2, "B", None)])
        pat = {"alt": [{"seq": [var("A", eq_kind("A")), var("B", eq_kind("B"))]},
                       {"seq": [var("A", eq_kind("A")), var("C", eq_kind("C"))]}]}
        res = run_query(conn, pattern=pat, **Q)
        self.assertEqual(res.matches[0].captures, {"A": [1], "B": [], "C": [2]})
        self.assertEqual(res.matches[1].captures, {"A": [3], "B": [4], "C": []})

    def test_d3_no_over_merge(self):
        conn = make_conn([(1, "p", 1, "BX", 1), (2, "p", 2, "BX", 2),
                          (3, "p", 3, "C", 3)])
        bx = eq_kind("BX")
        c = {"and": [eq_kind("C"),
                     {"cmp": {"op": "eq",
                              "left": {"last": {"var": "B", "field": "v"}},
                              "right": {"lit": 2}}}]}
        pat = {"seq": [{"alt": [{"seq": [var("B", bx), var("X", bx)]},
                                {"seq": [var("X", bx), var("B", bx)]}]},
                       var("C", c)]}
        for memo in (True, False):
            res = run_query(conn, pattern=pat, use_memo=memo, **Q)
            self.assertEqual(len(res.matches), 1)
            self.assertEqual(res.matches[0].captures["B"], [2])
        naive = run_query(conn, pattern=pat, naive_state_merge=True, **Q)
        self.assertEqual(len(naive.matches), 0)  # diagnostic: unsound merge

    def test_d4_reluctant_vs_greedy(self):
        conn = make_conn([(1, "p", 1, "A", None), (2, "p", 2, "B", None),
                          (3, "p", 3, "B", None)])
        def pat(mode):
            return {"seq": [var("A", eq_kind("A")),
                            {"rep": {"of": var("B", eq_kind("B")), "min": 0,
                                     "max": None, "mode": mode}}]}
        g = run_query(conn, pattern=pat("greedy"), **Q).matches[0]
        r = run_query(conn, pattern=pat("reluctant"), **Q).matches[0]
        self.assertEqual(g.captures["B"], [2, 3])
        self.assertEqual(r.captures["B"], [])
        self.assertEqual(r.end_row_id, 1)

    def test_d5_overlapping_next_row(self):
        conn = make_conn([(1, "p", 1, "X", None), (2, "p", 2, "X", None),
                          (3, "p", 3, "X", None)])
        pat = {"seq": [var("X"), var("X")]}
        res = run_query(conn, pattern=pat, skip="next_row", **Q)
        self.assertEqual([(m.start_row_id, m.end_row_id) for m in res.matches],
                         [(1, 2), (2, 3)])
        self.assertEqual(res.matches[0].captures["X"], [1, 2])

    def test_d6_to_first_failures(self):
        conn = make_conn([(1, "p", 1, "A", None), (2, "p", 2, "B", None)])
        pat = {"seq": [var("A", eq_kind("A")), var("B", eq_kind("B"))]}
        with self.assertRaises(QueryError):
            run_query(conn, pattern=pat, skip="to_first:Q", **Q)
        with self.assertRaises(QueryError):
            run_query(conn, pattern=pat, skip="to_first:A", **Q)

    def test_d6_to_first_success(self):
        conn = make_conn([(i, "p", i, k, None)
                          for i, k in enumerate("ABCABC", 1)])
        pat = {"seq": [var("A", eq_kind("A")),
                       {"rep": {"of": var("B", eq_kind("B")), "min": 0, "max": None}},
                       var("C", eq_kind("C"))]}
        res = run_query(conn, pattern=pat, skip="to_first:C", **Q)
        self.assertEqual([(m.start_row_id, m.end_row_id, m.next_start_row_id)
                          for m in res.matches], [(1, 3, 3), (4, 6, 6)])

    def test_d9_partition_and_tiebreak(self):
        conn = make_conn([(1, "p1", 10, "A", None), (3, "p1", 10, "B", None),
                          (2, "p2", 10, "A", None), (4, "p2", 10, "B", None),
                          (5, "p3", 7, "B", None), (6, "p3", 7, "A", None)])
        pat = {"seq": [var("A", eq_kind("A")), var("B", eq_kind("B"))]}
        res = run_query(conn, pattern=pat, **Q)
        self.assertEqual({(m.partition, m.start_row_id, m.end_row_id)
                          for m in res.matches}, {("p1", 1, 3), ("p2", 2, 4)})

    def test_row_id_uniqueness_enforced(self):
        conn = make_conn([(1, "p", 1, "A", None), (1, "p", 2, "B", None)])
        with self.assertRaises(QueryError):
            run_query(conn, pattern=var("A"), **Q)


# ---------------------------------------------------------------------------
# randomized cross-check: engine vs exhaustive interpreter
# ---------------------------------------------------------------------------

def _rand_cond(rng, depth):
    choices = ["kind", "vcmp", "isnull", "last", "count"]
    if depth > 0:
        choices += ["and", "or", "not"]
    c = rng.choice(choices)
    if c == "kind":
        return eq_kind(rng.choice("ABC"))
    if c == "vcmp":
        return {"cmp": {"op": rng.choice(["lt", "le", "gt", "ge", "eq", "ne"]),
                        "left": {"field": "v"}, "right": {"lit": rng.randint(0, 3)}}}
    if c == "isnull":
        return {"is_null": {"field": "v"}}
    if c == "last":
        return {"cmp": {"op": rng.choice(["eq", "lt", "gt"]),
                        "left": {"last": {"var": rng.choice("PQ"), "field": "v"}},
                        "right": {"lit": rng.randint(0, 3)}}}
    if c == "count":
        return {"cmp": {"op": rng.choice(["lt", "eq", "le"]),
                        "left": {"count": {"var": rng.choice("PQ")}},
                        "right": {"lit": rng.randint(0, 2)}}}
    if c == "and":
        return {"and": [_rand_cond(rng, depth - 1) for _ in range(rng.randint(1, 2))]}
    if c == "or":
        return {"or": [_rand_cond(rng, depth - 1) for _ in range(rng.randint(1, 2))]}
    return {"not": _rand_cond(rng, depth - 1)}


def _rand_pattern(rng, depth):
    if depth == 0 or rng.random() < 0.45:
        return var(rng.choice("PQ"), _rand_cond(rng, 1))
    c = rng.choice(["seq", "alt", "rep"])
    if c == "seq":
        return {"seq": [_rand_pattern(rng, depth - 1)
                        for _ in range(rng.randint(1, 3))]}
    if c == "alt":
        return {"alt": [_rand_pattern(rng, depth - 1) for _ in range(2)]}
    hi = rng.choice([None, 1, 2, 3])
    lo = rng.randint(0, hi) if hi is not None else rng.randint(0, 1)
    return {"rep": {"of": _rand_pattern(rng, depth - 1), "min": lo, "max": hi,
                    "mode": rng.choice(["greedy", "reluctant"])}}


class TestFuzzCrossCheck(unittest.TestCase):
    def test_engine_matches_exhaustive(self):
        rng = random.Random(20261116)
        used = 0
        attempts = 0
        while used < 250 and attempts < 4000:
            attempts += 1
            spec = _rand_pattern(rng, 3)
            try:
                ast = parse_pattern(spec)
                validate_pattern(ast)
            except PatternError:
                continue
            used += 1
            n = rng.randint(3, 7)
            rows = [{"id": i + 1, "pk": "p", "ts": i,
                     "kind": rng.choice("ABC"),
                     "v": rng.choice([None, 0, 1, 2, 3])}
                    for i in range(n)]
            prog = compile_pattern(ast)
            live = compute_live_vars(prog)
            for memo in (True, False):
                for s in range(n):
                    m, _ = run_from(prog, live, rows, s, use_memo=memo)
                    em = exhaustive.first_match(ast, rows, s)
                    if m is None and em is None:
                        continue
                    self.assertIsNotNone(m, f"spec={spec} rows={rows} s={s}")
                    self.assertIsNotNone(em, f"spec={spec} rows={rows} s={s}")
                    self.assertEqual(m.start, em[0], f"spec={spec} rows={rows} s={s}")
                    self.assertEqual(m.end, em[1], f"spec={spec} rows={rows} s={s}")
                    self.assertEqual({v: list(x) for v, x in m.captures.items()},
                                     {v: list(x) for v, x in em[2].items()},
                                     f"spec={spec} rows={rows} s={s}")
        self.assertGreaterEqual(used, 250)


if __name__ == "__main__":
    unittest.main()

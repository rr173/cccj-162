"""Batch query API over SQLite.

run_query(): execute one row-sequence query over a finite event table.
cross_check(): verify the engine against the independent exhaustive
interpreter on small tables, item by item.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .pattern import (parse_pattern, validate_pattern, compile_pattern,
                      compute_live_vars, pattern_vars, walk, Var, PatternError)
from .conditions import cond_fields
from .engine import run_from, EngineStats
from . import exhaustive


class QueryError(RuntimeError):
    """Raised when a query fails at run time (e.g. a skip strategy that
    cannot advance) or when table/column configuration is invalid."""


# ---------------------------------------------------------------------------
# result model
# ---------------------------------------------------------------------------

@dataclass
class Match:
    partition: object                 # partition key value
    start_row_id: object              # first consumed row
    end_row_id: object                # last consumed row
    captures: dict                    # var -> [row_id, ...] in consumption order
    measures: dict                    # var -> {count, first_row_id, last_row_id}
    next_start_row_id: Optional[object]  # where matching resumes (None = partition exhausted)
    rows: Optional[list]              # the original consumed rows (if include_rows)

    def to_dict(self) -> dict:
        return {
            "partition": self.partition,
            "start_row_id": self.start_row_id,
            "end_row_id": self.end_row_id,
            "captures": self.captures,
            "measures": self.measures,
            "next_start_row_id": self.next_start_row_id,
            "rows": self.rows,
        }


@dataclass
class QueryResult:
    matches: list
    stats: dict


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _qi(name) -> str:
    """Quote an SQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def normalize_skip(skip):
    """Normalize a skip specification to (strategy, variable|None).

    Accepted forms: "past_last" | "next_row" | "to_first:VAR" |
    ("to_first", "VAR") | {"strategy": ..., "variable": ...}.
    """
    if isinstance(skip, str):
        if skip in ("past_last", "next_row"):
            return skip, None
        if skip.startswith("to_first:") and skip[len("to_first:"):]:
            return "to_first", skip[len("to_first:"):]
    elif isinstance(skip, (tuple, list)) and len(skip) == 2 and skip[0] == "to_first" and skip[1]:
        return "to_first", skip[1]
    elif isinstance(skip, dict):
        strat = skip.get("strategy")
        if strat in ("past_last", "next_row"):
            return strat, None
        if strat == "to_first" and skip.get("variable"):
            return "to_first", skip["variable"]
    raise QueryError(
        f"invalid skip specification {skip!r}; expected 'past_last', 'next_row' "
        "or to_first:<variable>")


def _load_partitions(conn, table, partition_key, order_value, row_id):
    """Load the table ordered by (partition_key, order_value, row_id) and
    group consecutive equal partition keys.  Rows are dicts."""
    cur = conn.execute(
        f"SELECT * FROM {_qi(table)} "
        f"ORDER BY {_qi(partition_key)}, {_qi(order_value)}, {_qi(row_id)}")
    names = [d[0] for d in cur.description]
    parts = []
    current_key = object()  # sentinel: never equal to a real key
    current_rows = None
    for raw in cur:
        row = dict(zip(names, raw))
        key = row[partition_key]
        if current_rows is None or key != current_key:
            current_key = key
            current_rows = []
            parts.append((key, current_rows))
        current_rows.append(row)
    return parts


def _check_table(conn, table, partition_key, order_value, row_id, ast):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_qi(table)})")]
    if not cols:
        raise QueryError(f"table {table!r} does not exist or has no columns")
    for c in (partition_key, order_value, row_id):
        if c not in cols:
            raise QueryError(f"column {c!r} not found in table {table!r} (has {cols})")
    used = set()
    for node in walk(ast):
        if isinstance(node, Var):
            used |= cond_fields(node.cond)
    missing = used - set(cols)
    if missing:
        raise QueryError(f"conditions reference unknown columns: {sorted(missing)} "
                         f"(table has {cols})")
    dup = conn.execute(
        f"SELECT {_qi(row_id)} FROM {_qi(table)} "
        f"GROUP BY 1 HAVING COUNT(*) > 1 LIMIT 1").fetchone()
    if dup:
        raise QueryError(f"row_id column {row_id!r} is not globally unique; "
                         f"duplicate value: {dup[0]!r}")


def _partition_loop(first_fn, rows, skip_spec, row_id):
    """The per-partition matching loop, shared by run_query and cross_check.

    first_fn(start_index) -> (start, end, captures) | None  where captures
    maps var -> sequence of row indices.  Returns a list of
    (start, end, captures, next_start_index).
    """
    out = []
    n = len(rows)
    idx = 0
    while idx < n:
        r = first_fn(idx)
        if r is None:
            idx += 1          # no complete pattern from this row: move on
            continue
        start, end, caps = r
        strat, var = skip_spec
        if strat == "past_last":
            nxt = end
        elif strat == "next_row":
            nxt = start + 1
        else:  # to_first
            lst = caps.get(var)
            if not lst:
                raise QueryError(
                    f"skip to_first({var!r}): variable was not captured in the "
                    f"match starting at row_id={rows[start][row_id]!r}; "
                    "the query fails (no strategy is substituted)")
            nxt = lst[0]
            if nxt <= start:
                raise QueryError(
                    f"skip to_first({var!r}): first capture row_id="
                    f"{rows[nxt][row_id]!r} does not advance past the current "
                    f"start row_id={rows[start][row_id]!r}; the query fails "
                    "(refusing to loop or silently change strategy)")
        out.append((start, end, caps, nxt))
        idx = nxt
    return out


# ---------------------------------------------------------------------------
# main entry points
# ---------------------------------------------------------------------------

def run_query(conn, *, table, pattern, partition_key, order_value, row_id,
              skip="past_last", use_memo=True, naive_state_merge=False,
              max_steps=None, max_program_nodes=100_000, include_rows=True):
    """Execute a row-sequence query.

    conn:           sqlite3 connection
    table:          finite event table
    pattern:        pattern protocol dict (or an already-parsed AST)
    partition_key / order_value / row_id: column names; rows are matched per
                    partition in (order_value, row_id) total order
    skip:           "past_last" | "next_row" | ("to_first", "VAR") | "to_first:VAR"
    Returns QueryResult(matches=[Match, ...], stats={...}).
    Raises PatternError for invalid patterns, QueryError for table/skip
    failures, StepLimitError when a start exceeds max_steps.
    """
    ast = parse_pattern(pattern) if isinstance(pattern, dict) else pattern
    validate_pattern(ast, max_program_nodes)
    _check_table(conn, table, partition_key, order_value, row_id, ast)
    skip_spec = normalize_skip(skip)
    prog = compile_pattern(ast)
    live = compute_live_vars(prog)
    all_vars = sorted(pattern_vars(ast))

    stats = EngineStats()
    matches = []
    rows_scanned = 0
    parts = _load_partitions(conn, table, partition_key, order_value, row_id)
    for pval, rows in parts:
        rows_scanned += len(rows)

        def first_fn(s, _rows=rows):
            m, st = run_from(prog, live, _rows, s,
                             use_memo=use_memo,
                             naive_state_merge=naive_state_merge,
                             max_steps=max_steps)
            stats.absorb(st)
            if m is None:
                return None
            return m.start, m.end, m.captures

        for start, end, caps, nxt in _partition_loop(first_fn, rows, skip_spec, row_id):
            cap_ids = {v: [rows[k][row_id] for k in ix] for v, ix in caps.items()}
            for v in all_vars:
                cap_ids.setdefault(v, [])
            measures = {
                v: {"count": len(ids),
                    "first_row_id": ids[0] if ids else None,
                    "last_row_id": ids[-1] if ids else None}
                for v, ids in cap_ids.items()
            }
            matches.append(Match(
                partition=pval,
                start_row_id=rows[start][row_id],
                end_row_id=rows[end - 1][row_id],
                captures=cap_ids,
                measures=measures,
                next_start_row_id=rows[nxt][row_id] if nxt < len(rows) else None,
                rows=[dict(r) for r in rows[start:end]] if include_rows else None,
            ))
    return QueryResult(matches=matches, stats={
        **stats.to_dict(),
        "partitions": len(parts),
        "rows_scanned": rows_scanned,
        "matches": len(matches),
    })


def cross_check(conn, *, table, pattern, partition_key, order_value, row_id,
                skip="past_last", use_memo=True, max_steps=None):
    """Verify the engine against the independent exhaustive interpreter.

    For every partition and every start row: the engine's first match must
    equal the exhaustive interpreter's first-listed path (end + captures).
    The whole match stream (with the skip strategy) must also agree,
    including every next-start position.  Returns a report dict.
    """
    ast = parse_pattern(pattern) if isinstance(pattern, dict) else pattern
    validate_pattern(ast)
    _check_table(conn, table, partition_key, order_value, row_id, ast)
    skip_spec = normalize_skip(skip)
    prog = compile_pattern(ast)
    live = compute_live_vars(prog)

    checks = []
    mismatches = []
    parts = _load_partitions(conn, table, partition_key, order_value, row_id)
    for pval, rows in parts:
        # 1) per-start comparison
        for s in range(len(rows)):
            m, _st = run_from(prog, live, rows, s, use_memo=use_memo,
                              max_steps=max_steps)
            em = exhaustive.first_match(ast, rows, s)
            ok = _same_match(m, em)
            rec = {"partition": pval, "start_row_id": rows[s][row_id], "ok": ok}
            if not ok:
                rec["engine"] = None if m is None else (m.start, m.end, m.captures)
                rec["exhaustive"] = em
                mismatches.append(rec)
            checks.append(rec)
        # 2) whole-stream comparison (boundaries, captures, next starts)
        eng_stream = _partition_loop(
            lambda s: (lambda m: None if m is None else (m.start, m.end, m.captures))
                      (run_from(prog, live, rows, s, use_memo=use_memo,
                                max_steps=max_steps)[0]),
            rows, skip_spec, row_id)
        exh_stream = _partition_loop(
            lambda s: exhaustive.first_match(ast, rows, s),
            rows, skip_spec, row_id)
        stream_ok = _same_stream(eng_stream, exh_stream)
        checks.append({"partition": pval, "stream": True, "ok": stream_ok,
                       "engine_matches": len(eng_stream),
                       "exhaustive_matches": len(exh_stream)})
        if not stream_ok:
            mismatches.append(checks[-1])
    return {"ok": not mismatches, "checks": checks, "mismatches": mismatches}


def _norm_caps(caps):
    return {v: list(ix) for v, ix in caps.items()}


def _same_match(m, em):
    if m is None and em is None:
        return True
    if m is None or em is None:
        return False
    return m.start == em[0] and m.end == em[1] and _norm_caps(m.captures) == _norm_caps(em[2])


def _same_stream(a, b):
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        if ra[0] != rb[0] or ra[1] != rb[1] or ra[3] != rb[3]:
            return False
        if _norm_caps(ra[2]) != _norm_caps(rb[2]):
            return False
    return True

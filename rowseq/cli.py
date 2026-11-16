"""Command line interface for the row-sequence query operator.

Usage:
    python3 -m rowseq.cli run   --db D --table T --pattern-file P.json \
        --partition-key C --order-value C --row-id C [--skip S] [--no-memo]
    python3 -m rowseq.cli check ...same options...   # engine vs exhaustive
    python3 -m rowseq.cli demo                        # demonstration suite
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from .api import run_query, cross_check, QueryError
from .pattern import PatternError
from .conditions import ConditionError
from .engine import StepLimitError


def _load_pattern(args):
    if args.pattern_file:
        with open(args.pattern_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(args.pattern)


def _add_common(sp):
    sp.add_argument("--db", required=True, help="SQLite database file")
    sp.add_argument("--table", required=True, help="event table name")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--pattern", help="pattern JSON string")
    g.add_argument("--pattern-file", help="path to a pattern JSON file")
    sp.add_argument("--partition-key", required=True, help="partition column")
    sp.add_argument("--order-value", required=True, help="ordering column")
    sp.add_argument("--row-id", required=True, help="globally unique row id column")
    sp.add_argument("--skip", default="past_last",
                    help="past_last | next_row | to_first:VAR (default past_last)")
    sp.add_argument("--no-memo", action="store_true",
                    help="disable failed-state sharing")
    sp.add_argument("--max-steps", type=int, default=None,
                    help="per-start VM step budget")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="rowseq",
        description="Row-sequence query operator (batch, MATCH_RECOGNIZE-style)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp_run = sub.add_parser("run", help="run a row-sequence query")
    _add_common(sp_run)
    sp_run.add_argument("--format", choices=["text", "json"], default="text")

    sp_chk = sub.add_parser("check",
                            help="cross-check engine vs exhaustive interpreter "
                                 "(small tables)")
    _add_common(sp_chk)

    sub.add_parser("demo", help="run the demonstration suite")

    args = parser.parse_args(argv)

    if args.cmd == "demo":
        from .demos import run_all_demos
        run_all_demos()
        return 0

    try:
        pattern = _load_pattern(args)
        conn = sqlite3.connect(args.db)
        try:
            if args.cmd == "run":
                res = run_query(
                    conn, table=args.table, pattern=pattern,
                    partition_key=args.partition_key, order_value=args.order_value,
                    row_id=args.row_id, skip=args.skip,
                    use_memo=not args.no_memo, max_steps=args.max_steps)
                if args.format == "json":
                    print(json.dumps(
                        {"matches": [m.to_dict() for m in res.matches],
                         "stats": res.stats},
                        ensure_ascii=False, indent=2, default=str))
                else:
                    for k, m in enumerate(res.matches, 1):
                        print(f"match #{k} partition={m.partition!r} "
                              f"start={m.start_row_id} end={m.end_row_id} "
                              f"next_start={m.next_start_row_id}")
                        for v in sorted(m.captures):
                            mm = m.measures[v]
                            print(f"    {v}: rows={m.captures[v]} "
                                  f"count={mm['count']} first={mm['first_row_id']} "
                                  f"last={mm['last_row_id']}")
                        if m.rows:
                            for r in m.rows:
                                print(f"    row: {r}")
                    print(f"stats: {res.stats}")
            else:  # check
                rep = cross_check(
                    conn, table=args.table, pattern=pattern,
                    partition_key=args.partition_key, order_value=args.order_value,
                    row_id=args.row_id, skip=args.skip,
                    use_memo=not args.no_memo, max_steps=args.max_steps)
                n_ok = sum(1 for c in rep["checks"] if c["ok"])
                print(f"{n_ok}/{len(rep['checks'])} checks passed")
                if not rep["ok"]:
                    print(json.dumps(rep["mismatches"], indent=2, default=str))
                    return 1
        finally:
            conn.close()
    except (PatternError, ConditionError, QueryError, StepLimitError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

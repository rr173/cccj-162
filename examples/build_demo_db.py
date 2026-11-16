#!/usr/bin/env python3
"""Build the example SQLite database used by the README commands.

Usage:  python3 examples/build_demo_db.py [output.db]   (default: examples/demo.db)

Table `events`: one quality-inspection stream per lot (partition key `lot`),
ordered by (ts, id).  Each lot is an episode: one `enter`, some `check`s,
then a final `pass` or `exit` -- plus some noise lots that never complete.
"""

import os
import sqlite3
import sys

ROWS = [
    # id, lot, ts, kind, v
    (1,  "L1", 1, "enter", None),
    (2,  "L1", 2, "check", 91),
    (3,  "L1", 3, "check", 88),
    (4,  "L1", 4, "pass",  None),
    (5,  "L2", 1, "enter", None),
    (6,  "L2", 2, "check", 55),
    (7,  "L2", 3, "exit",  None),
    (8,  "L3", 1, "enter", None),
    (9,  "L3", 2, "check", 70),
    # L3 never terminates -> no match for that lot
    (10, "L4", 1, "check", 10),   # no enter -> no match
    (11, "L4", 2, "pass",  None),
    (12, "L5", 5, "enter", None), # same timestamps inside one partition:
    (13, "L5", 5, "check", 77),   # (ts, id) total order decides
    (14, "L5", 5, "pass",  None),
    # classic A B* C stream in its own partition
    (20, "AB", 1, "A", None),
    (21, "AB", 2, "B", 20),
    (22, "AB", 3, "B", 30),
    (23, "AB", 4, "C", 40),
]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "demo.db")
    if os.path.exists(out):
        os.remove(out)
    conn = sqlite3.connect(out)
    conn.execute("CREATE TABLE events ("
                 "  id INTEGER PRIMARY KEY,"
                 "  lot TEXT NOT NULL,"
                 "  ts INTEGER NOT NULL,"
                 "  kind TEXT NOT NULL,"
                 "  v INTEGER)")
    conn.executemany("INSERT INTO events (id, lot, ts, kind, v) VALUES (?,?,?,?,?)", ROWS)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    print(f"wrote {out} with {n} rows")


if __name__ == "__main__":
    main()

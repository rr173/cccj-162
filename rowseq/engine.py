"""Matching engine: an iterative depth-first backtracking VM.

Properties required by the operator contract:

* No Python recursion: unbounded repetition over long partitions cannot
  overflow the call stack.  Choice points live on an explicit heap stack.
* Laziness: paths are explored one at a time in the public priority order
  (alternation left-first; greedy = iterate-before-exit; reluctant =
  exit-before-iterate).  The search stops at the first ACCEPT, so only the
  first complete path for a start is returned -- never all backtracking
  paths, and never a "longest prefix" whose suffix would have failed.
* Each consumed row is assigned to exactly one variable; rows are consumed
  strictly in order (no skipping of non-matching rows).
* Captures use a trail (undo log), so choice points are O(1) and memory is
  linear in the longest simultaneous path, not in the number of paths.
* Optional candidate-state sharing (memoization of failed states): two
  candidates may only share a state when their capture histories are
  equivalent for everything the future can observe.  The key is
  (program counter, row index, captures of the live variables at that pc).
  A naive (pc, row) merge is UNSOUND -- see run_from(naive_state_merge=True),
  kept only as a diagnostic that demonstrates the failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass

from .conditions import eval_cond


class StepLimitError(RuntimeError):
    """Raised when a start exceeds the configured step budget."""


@dataclass
class EngineStats:
    steps: int = 0               # VM instructions executed
    backtracks: int = 0          # choice points popped
    max_choice_points: int = 0   # deepest backtracking stack
    memo_states: int = 0         # largest failure-memo size seen
    memo_hits: int = 0           # states pruned via the memo

    def absorb(self, other: "EngineStats") -> None:
        self.steps += other.steps
        self.backtracks += other.backtracks
        self.max_choice_points = max(self.max_choice_points, other.max_choice_points)
        self.memo_states = max(self.memo_states, other.memo_states)
        self.memo_hits += other.memo_hits

    def to_dict(self) -> dict:
        return {
            "steps": self.steps,
            "backtracks": self.backtracks,
            "max_choice_points": self.max_choice_points,
            "memo_states": self.memo_states,
            "memo_hits": self.memo_hits,
        }


@dataclass
class EngineMatch:
    start: int        # row index within the partition (inclusive)
    end: int          # row index within the partition (exclusive)
    captures: dict    # var -> [row indices] in consumption order


def run_from(prog, live, rows, start, *,
             use_memo: bool = True,
             naive_state_merge: bool = False,
             max_steps=None):
    """Find the first complete match starting at row index `start`.

    Returns (EngineMatch | None, EngineStats).  `rows` is the partition as a
    list of dicts; matching never crosses the partition end.
    """
    n = len(rows)
    st = EngineStats()
    captures = {}     # var -> list of row indices (mutable, trailed)
    trail = []        # undo log of variable names, parallel to captures
    stack = []        # choice points: (pc, row_index, trail_len, memo_key|None)
    memo = set()      # keys of states proven to have no complete match
    pc, i = 0, start

    def signature(pc_):
        # Everything the future can observe: captures of the variables that
        # are live (referenced by reachable conditions) at this pc.
        if naive_state_merge:
            return ()
        lv = live[pc_]
        if not lv:
            return ()
        return tuple((v, tuple(captures.get(v, ()))) for v in sorted(lv))

    def backtrack():
        nonlocal pc, i
        while stack:
            t_pc, t_i, t_len, mkey = stack.pop()
            while len(trail) > t_len:
                captures[trail.pop()].pop()
            st.backtracks += 1
            if mkey is not None:
                # Popping the LAST alternative of a split state: from here on,
                # either a complete match is found below (search ends) or the
                # state truly fails -- so recording it as failed is sound.
                if mkey in memo:
                    st.memo_hits += 1
                    continue  # equivalent state already failed; next choice
                memo.add(mkey)
            pc, i = t_pc, t_i
            return True
        return False

    def finish():
        if len(memo) > st.memo_states:
            st.memo_states = len(memo)

    while True:
        st.steps += 1
        if max_steps is not None and st.steps > max_steps:
            raise StepLimitError(
                f"step budget {max_steps} exceeded at start row {start}; "
                "the pattern/input combination explores too many paths")
        op = prog[pc]
        kind = op[0]

        if kind == "accept":
            finish()
            return EngineMatch(start, i,
                               {v: list(ix) for v, ix in captures.items()}), st

        if kind == "jmp":
            pc = op[1]
            continue

        if kind == "split":
            targets = op[1]
            mkey = None
            if use_memo:
                mkey = (pc, i, signature(pc))
                if mkey in memo:
                    st.memo_hits += 1
                    if not backtrack():
                        finish()
                        return None, st
                    continue
            # try targets[0] now; push the rest (deepest = last alternative,
            # which carries the memo key of this split state)
            last = len(targets) - 1
            for t in range(last, 0, -1):
                stack.append((targets[t], i, len(trail),
                              mkey if (use_memo and t == last) else None))
            if len(stack) > st.max_choice_points:
                st.max_choice_points = len(stack)
            pc = targets[0]
            continue

        # kind == "consume"
        _, var, cond = op
        ckey = None
        if use_memo:
            ckey = (pc, i, signature(pc))
            if ckey in memo:
                st.memo_hits += 1
                if not backtrack():
                    finish()
                    return None, st
                continue
        if i < n and eval_cond(cond, rows[i], captures, rows) is True:
            captures.setdefault(var, []).append(i)   # row joins captures only
            trail.append(var)                        # AFTER the condition passed
            i += 1
            pc += 1
            continue
        if use_memo:
            memo.add(ckey)   # this consume can never succeed from this state
        if not backtrack():
            finish()
            return None, st

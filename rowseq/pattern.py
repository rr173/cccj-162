"""Pattern protocol: AST, validation, compilation to VM instructions.

Pattern protocol (JSON)
-----------------------
    {"var": "NAME", "cond": CONDITION}      leaf: consume exactly one row into NAME
                                            ("cond" optional, default always true)
    {"seq": [P, ...]}                       concatenation
    {"alt": [P, ...]}                       alternation (left branch has priority)
    {"rep": {"of": P, "min": m, "max": M, "mode": "greedy"|"reluctant"}}
                                            bounded/unbounded repetition;
                                            min default 0, max default null (unbounded),
                                            mode default "greedy".
    {"seq": []}                             the empty branch (matches no rows)

Validation rules (rejected before execution)
--------------------------------------------
* The whole pattern must not be able to match an empty row sequence
  (otherwise every position would yield an empty match forever).
* The repeated sub-pattern of any "rep" node must not be able to match
  empty (otherwise a repetition could loop without consuming rows).
* Bounded repetitions are compiled by expansion; the compiled program size
  is limited (default 100_000 instructions).

Compilation target
------------------
A flat list of instructions:
    ("consume", var, cond)   consume next row if cond is True, else fail
    ("split", (t1, t2, ...)) ordered choice: try t1 first, then t2, ...
    ("jmp", t)
    ("accept",)
Greedy loops try "one more iteration" before "exit"; reluctant loops the
opposite.  The engine is a depth-first backtracking VM over this program,
so the first ACCEPT it reaches is exactly the first complete match in the
public priority order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .conditions import validate_cond, cond_refs


class PatternError(ValueError):
    """Raised for invalid pattern configurations."""


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Var:
    name: str
    cond: dict


@dataclass(frozen=True)
class Seq:
    children: Tuple[object, ...]


@dataclass(frozen=True)
class Alt:
    children: Tuple[object, ...]


@dataclass(frozen=True)
class Rep:
    child: object
    lo: int                 # minimum repetitions (>= 0)
    hi: Optional[int]       # maximum repetitions, None = unbounded
    mode: str               # "greedy" | "reluctant"


def _check_count(name, value) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PatternError(f"repetition '{name}' must be a non-negative integer, got: {value!r}")


def parse_pattern(spec):
    """Parse the JSON pattern protocol into an AST (with validation)."""
    if not isinstance(spec, dict):
        raise PatternError(f"pattern node must be an object, got: {spec!r}")
    kinds = [k for k in ("var", "seq", "alt", "rep") if k in spec]
    if len(kinds) != 1:
        raise PatternError(
            f"pattern node must contain exactly one of var/seq/alt/rep, got: {spec!r}")
    if "var" in spec:
        if set(spec) - {"var", "cond"}:
            raise PatternError(f"a 'var' node may only carry 'var' and 'cond', got: {spec!r}")
        name = spec["var"]
        if not isinstance(name, str) or not name:
            raise PatternError(f"variable name must be a non-empty string, got: {name!r}")
        cond = spec.get("cond", {"const": True})
        validate_cond(cond)
        return Var(name, cond)
    if "seq" in spec:
        children = spec["seq"]
        if not isinstance(children, list):
            raise PatternError(f"'seq' must be a list, got: {spec!r}")
        return Seq(tuple(parse_pattern(c) for c in children))
    if "alt" in spec:
        children = spec["alt"]
        if not isinstance(children, list) or not children:
            raise PatternError(f"'alt' must be a non-empty list, got: {spec!r}")
        return Alt(tuple(parse_pattern(c) for c in children))
    if "rep" in spec:
        r = spec["rep"]
        if not isinstance(r, dict) or "of" not in r:
            raise PatternError(f"'rep' must be an object with an 'of' key, got: {spec!r}")
        extra = set(r) - {"of", "min", "max", "mode"}
        if extra:
            raise PatternError(f"unknown 'rep' keys: {sorted(extra)}")
        child = parse_pattern(r["of"])
        lo = r.get("min", 0)
        hi = r.get("max", None)
        mode = r.get("mode", "greedy")
        _check_count("min", lo)
        if hi is not None:
            _check_count("max", hi)
            if hi < lo:
                raise PatternError(f"repetition max ({hi}) < min ({lo})")
        if mode not in ("greedy", "reluctant"):
            raise PatternError(f"repetition mode must be 'greedy' or 'reluctant', got: {mode!r}")
        return Rep(child, lo, hi, mode)
    raise PatternError(f"unknown pattern node: {spec!r}")


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def nullable(node) -> bool:
    """Can this node match an empty row sequence?"""
    if isinstance(node, Var):
        return False
    if isinstance(node, Seq):
        return all(nullable(c) for c in node.children)
    if isinstance(node, Alt):
        return any(nullable(c) for c in node.children)
    if isinstance(node, Rep):
        return node.lo == 0 or nullable(node.child)
    raise PatternError(f"unknown node: {node!r}")


def walk(node):
    """Yield the node and all descendants."""
    yield node
    if isinstance(node, (Seq, Alt)):
        for c in node.children:
            yield from walk(c)
    elif isinstance(node, Rep):
        yield from walk(node.child)


def pattern_vars(root) -> set:
    """All variable names defined (consumed into) by the pattern."""
    return {n.name for n in walk(root) if isinstance(n, Var)}


def _estimate(node) -> int:
    """Rough compiled-program-size estimate (for the expansion budget)."""
    if isinstance(node, Var):
        return 1
    if isinstance(node, Seq):
        return sum(_estimate(c) for c in node.children)
    if isinstance(node, Alt):
        return 1 + sum(_estimate(c) + 1 for c in node.children)
    if isinstance(node, Rep):
        copies = node.hi if node.hi is not None else node.lo + 1
        return copies * (_estimate(node.child) + 2) + 1
    raise PatternError(f"unknown node: {node!r}")


def validate_pattern(root, max_program_nodes: int = 100_000) -> None:
    """Reject configurations that must not execute."""
    if nullable(root):
        raise PatternError(
            "the whole pattern can match an empty row sequence; "
            "it would produce empty matches at every position -- rejected")
    for node in walk(root):
        if isinstance(node, Rep) and nullable(node.child):
            raise PatternError(
                "the repeated sub-pattern of a repetition can match empty; "
                "the repetition could loop without consuming rows -- rejected")
    size = _estimate(root)
    if size > max_program_nodes:
        raise PatternError(
            f"pattern compiles to ~{size} instructions, limit is {max_program_nodes}; "
            "reduce bounded repetition counts")


# ---------------------------------------------------------------------------
# compilation
# ---------------------------------------------------------------------------

def compile_pattern(root):
    """Compile the AST to a flat instruction list (see module docstring)."""
    prog = []

    def emit(op):
        prog.append(op)
        return len(prog) - 1

    def gen(node):
        if isinstance(node, Var):
            emit(("consume", node.name, node.cond))
        elif isinstance(node, Seq):
            for c in node.children:
                gen(c)
        elif isinstance(node, Alt):
            split_at = emit(("split", ()))
            targets = []
            end_jmps = []
            for c in node.children:
                targets.append(len(prog))
                gen(c)
                end_jmps.append(emit(("jmp", -1)))
            end = len(prog)
            prog[split_at] = ("split", tuple(targets))
            for j in end_jmps:
                prog[j] = ("jmp", end)
        elif isinstance(node, Rep):
            gen_rep(node)
        else:
            raise PatternError(f"unknown node: {node!r}")

    def gen_rep(node):
        # mandatory copies
        for _ in range(node.lo):
            gen(node.child)
        if node.hi is None:
            # unbounded loop: split prefers one more iteration (greedy)
            # or the exit (reluctant)
            loop_at = emit(("split", ()))
            body_at = len(prog)
            gen(node.child)
            emit(("jmp", loop_at))
            end = len(prog)
            prog[loop_at] = ("split", (body_at, end) if node.mode == "greedy"
                             else (end, body_at))
        else:
            # optional chain of (hi - lo) copies
            splits = []
            end_jmps = []
            for _ in range(node.hi - node.lo):
                s = emit(("split", ()))
                splits.append(s)
                gen(node.child)          # body starts right after the split
                end_jmps.append(emit(("jmp", -1)))
            end = len(prog)
            for s in splits:
                body_at = s + 1
                prog[s] = ("split", (body_at, end) if node.mode == "greedy"
                           else (end, body_at))
            for j in end_jmps:
                prog[j] = ("jmp", end)

    gen(root)
    emit(("accept",))
    return prog


def compute_live_vars(prog):
    """For every instruction: variables whose captures may still be read by
    conditions reachable from that instruction.  Used as the sound equivalence
    key for candidate-state merging: two histories may only share a state when
    everything the future can observe is identical."""
    n = len(prog)
    succ = [[] for _ in range(n)]
    refs = [set() for _ in range(n)]
    for i, op in enumerate(prog):
        kind = op[0]
        if kind == "consume":
            succ[i] = [i + 1]
            refs[i] = cond_refs(op[2])
        elif kind == "split":
            succ[i] = list(op[1])
        elif kind == "jmp":
            succ[i] = [op[1]]
        # "accept": no successors
    live = [set() for _ in range(n)]
    changed = True
    while changed:
        changed = False
        for i in range(n - 1, -1, -1):
            new = set(refs[i])
            for s in succ[i]:
                new |= live[s]
            if new != live[i]:
                live[i] = new
                changed = True
    return live

"""Independent exhaustive interpreter over the pattern AST.

Used to cross-check the VM engine on small tables: it enumerates ALL complete
matches of a start position in the public priority order (alternation
left-first; greedy = iterate-before-exit; reluctant = exit-before-iterate;
concatenation = depth-first lexicographic composition).  It is deliberately
simple and recursive -- small inputs only -- and shares no code with the
compiled-program engine, so agreement between the two is meaningful.
"""

from __future__ import annotations

from .pattern import Var, Seq, Alt, Rep
from .conditions import eval_cond


def enum_node(node, rows, i, captures):
    """Yield (end_index, captures) for every match of `node` at row `i`,
    in public priority order.  `captures` maps var -> tuple of row indices
    (treated as immutable; every yield produces a fresh dict)."""
    if isinstance(node, Var):
        if i < len(rows) and eval_cond(node.cond, rows[i], captures, rows) is True:
            nxt = dict(captures)
            nxt[node.name] = nxt.get(node.name, ()) + (i,)
            yield i + 1, nxt
        return
    if isinstance(node, Seq):
        yield from _enum_seq(node.children, 0, rows, i, captures)
        return
    if isinstance(node, Alt):
        for child in node.children:
            yield from enum_node(child, rows, i, captures)
        return
    if isinstance(node, Rep):
        yield from _enum_rep(node, rows, i, captures, 0)
        return
    raise TypeError(f"unknown node: {node!r}")


def _enum_seq(children, k, rows, i, captures):
    if k == len(children):
        yield i, captures
        return
    for i2, c2 in enum_node(children[k], rows, i, captures):
        yield from _enum_seq(children, k + 1, rows, i2, c2)


def _enum_rep(node, rows, i, captures, count):
    can_more = node.hi is None or count < node.hi
    can_exit = count >= node.lo
    if node.mode == "greedy":
        if can_more:
            for i2, c2 in enum_node(node.child, rows, i, captures):
                yield from _enum_rep(node, rows, i2, c2, count + 1)
        if can_exit:
            yield i, captures
    else:  # reluctant: exit before one more iteration
        if can_exit:
            yield i, captures
        if can_more:
            for i2, c2 in enum_node(node.child, rows, i, captures):
                yield from _enum_rep(node, rows, i2, c2, count + 1)


def first_match(root, rows, start):
    """First complete match at `start` -> (start, end, captures) or None."""
    for end, caps in enum_node(root, rows, start, {}):
        return start, end, caps
    return None


def all_matches(root, rows, start):
    """All complete matches at `start`, in public priority order."""
    return [(start, end, caps) for end, caps in enum_node(root, rows, start, {})]

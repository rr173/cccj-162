"""Structured conditions for the row-sequence query operator.

Condition protocol (JSON)
-------------------------
    {"const": true|false}
    {"is_null": OPERAND}
    {"not": CONDITION}
    {"and": [CONDITION, ...]}
    {"or":  [CONDITION, ...]}
    {"cmp": {"op": "eq|ne|lt|le|gt|ge", "left": OPERAND, "right": OPERAND}}

OPERAND:
    {"lit":   null | true|false | int64 | "text"}   literal
    {"field": "column"}                             field of the CURRENT row
    {"first": {"var": "V", "field": "column"}}      field of the FIRST row captured by V
    {"last":  {"var": "V", "field": "column"}}      field of the LAST  row captured by V
    {"count": {"var": "V"}}                         number of rows captured by V so far

Semantics
---------
* Types: int64 / text / bool / null.  Comparisons require identical types.
  A null operand, mixed types, ordering comparisons on bool, or unsupported
  Python values (float, bytes, ...) all make a comparison UNKNOWN.
* Three-valued (Kleene) logic: every condition evaluates to True / False /
  UNKNOWN (None).  Only True lets a variable consume the row.
* FIRST/LAST/COUNT of a variable with no captures yet evaluate to null.
  Referencing a variable that only appears later in the pattern is legal:
  it is simply still null; the engine never peeks at future rows.
* The current row is appended to a variable's captures only AFTER the
  condition passes, so a self reference (LAST/COUNT of the variable being
  defined) observes previous captures only -- never the current row.
"""

from __future__ import annotations

CMP_OPS = ("eq", "ne", "lt", "le", "gt", "ge")
ORDER_OPS = ("lt", "le", "gt", "ge")

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1


class ConditionError(ValueError):
    """Raised for malformed condition configurations."""


# ---------------------------------------------------------------------------
# validation / static analysis
# ---------------------------------------------------------------------------

def _is_int64(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and INT64_MIN <= v <= INT64_MAX


def validate_operand(op) -> None:
    if not isinstance(op, dict) or len(op) != 1:
        raise ConditionError(f"operand must be a single-key object, got: {op!r}")
    if "lit" in op:
        v = op["lit"]
        if v is not None and not isinstance(v, (bool, str)) and not _is_int64(v):
            raise ConditionError(
                f"literal must be null/bool/int64/text, got: {v!r}")
    elif "field" in op:
        if not isinstance(op["field"], str) or not op["field"]:
            raise ConditionError(f"'field' operand needs a column name, got: {op!r}")
    elif "first" in op or "last" in op:
        spec = op["first"] if "first" in op else op["last"]
        if not isinstance(spec, dict) or set(spec) != {"var", "field"} \
                or not isinstance(spec["var"], str) or not isinstance(spec["field"], str):
            raise ConditionError(f"first/last operand needs {{var, field}}, got: {op!r}")
    elif "count" in op:
        spec = op["count"]
        if not isinstance(spec, dict) or set(spec) != {"var"} or not isinstance(spec["var"], str):
            raise ConditionError(f"count operand needs {{var}}, got: {op!r}")
    else:
        raise ConditionError(f"unknown operand: {op!r}")


def validate_cond(cond) -> None:
    if not isinstance(cond, dict) or len(cond) != 1:
        raise ConditionError(f"condition must be a single-key object, got: {cond!r}")
    if "const" in cond:
        if not isinstance(cond["const"], bool):
            raise ConditionError(f"'const' condition must be a boolean, got: {cond!r}")
    elif "is_null" in cond:
        validate_operand(cond["is_null"])
    elif "not" in cond:
        validate_cond(cond["not"])
    elif "and" in cond or "or" in cond:
        key = "and" if "and" in cond else "or"
        items = cond[key]
        if not isinstance(items, list):
            raise ConditionError(f"'{key}' condition must be a list, got: {cond!r}")
        for c in items:
            validate_cond(c)
    elif "cmp" in cond:
        spec = cond["cmp"]
        if not isinstance(spec, dict) or set(spec) != {"op", "left", "right"} \
                or spec["op"] not in CMP_OPS:
            raise ConditionError(
                f"'cmp' needs {{op: eq|ne|lt|le|gt|ge, left, right}}, got: {cond!r}")
        validate_operand(spec["left"])
        validate_operand(spec["right"])
    else:
        raise ConditionError(f"unknown condition: {cond!r}")


def _operand_refs(op):
    """Variable names referenced by one operand."""
    if "first" in op:
        yield op["first"]["var"]
    elif "last" in op:
        yield op["last"]["var"]
    elif "count" in op:
        yield op["count"]["var"]


def cond_refs(cond) -> set:
    """All variable names whose captures the condition may read."""
    refs = set()

    def walk(c):
        if "cmp" in c:
            refs.update(_operand_refs(c["cmp"]["left"]))
            refs.update(_operand_refs(c["cmp"]["right"]))
        elif "is_null" in c:
            refs.update(_operand_refs(c["is_null"]))
        elif "and" in c:
            for x in c["and"]:
                walk(x)
        elif "or" in c:
            for x in c["or"]:
                walk(x)
        elif "not" in c:
            walk(c["not"])
        # "const" references nothing

    walk(cond)
    return refs


def cond_fields(cond) -> set:
    """All row-column names the condition may read (current row + captures)."""
    fields = set()

    def op_fields(op):
        if "field" in op:
            fields.add(op["field"])
        elif "first" in op:
            fields.add(op["first"]["field"])
        elif "last" in op:
            fields.add(op["last"]["field"])

    def walk(c):
        if "cmp" in c:
            op_fields(c["cmp"]["left"])
            op_fields(c["cmp"]["right"])
        elif "is_null" in c:
            op_fields(c["is_null"])
        elif "and" in c:
            for x in c["and"]:
                walk(x)
        elif "or" in c:
            for x in c["or"]:
                walk(x)
        elif "not" in c:
            walk(c["not"])

    walk(cond)
    return fields


# ---------------------------------------------------------------------------
# evaluation (three-valued: True / False / None=UNKNOWN)
# ---------------------------------------------------------------------------

def type_name(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int64"
    if isinstance(v, str):
        return "text"
    return "unsupported"


def eval_operand(op, row, captures, rows):
    """Evaluate an operand. `captures` maps var -> sequence of row indices."""
    if "lit" in op:
        return op["lit"]
    if "field" in op:
        return row.get(op["field"])
    if "first" in op or "last" in op:
        key = "first" if "first" in op else "last"
        spec = op[key]
        caps = captures.get(spec["var"])
        if not caps:
            return None
        idx = caps[0] if key == "first" else caps[-1]
        return rows[idx].get(spec["field"])
    if "count" in op:
        caps = captures.get(op["count"]["var"])
        if not caps:
            return None
        return len(caps)
    raise ConditionError(f"unknown operand: {op!r}")


def compare(op, left, right):
    """Three-valued comparison with strict same-type rules."""
    if left is None or right is None:
        return None
    tl, tr = type_name(left), type_name(right)
    if tl != tr or tl == "unsupported":
        return None
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if tl == "bool":
        return None  # no ordering on booleans
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    if op == "ge":
        return left >= right
    raise ConditionError(f"unknown comparison operator: {op!r}")


def eval_cond(cond, row, captures, rows):
    """Evaluate a condition -> True / False / None(UNKNOWN)."""
    if "const" in cond:
        return True if cond["const"] else False
    if "is_null" in cond:
        return eval_operand(cond["is_null"], row, captures, rows) is None
    if "not" in cond:
        v = eval_cond(cond["not"], row, captures, rows)
        return None if v is None else (not v)
    if "and" in cond:
        unknown = False
        for c in cond["and"]:
            v = eval_cond(c, row, captures, rows)
            if v is False:
                return False
            if v is None:
                unknown = True
        return None if unknown else True
    if "or" in cond:
        unknown = False
        for c in cond["or"]:
            v = eval_cond(c, row, captures, rows)
            if v is True:
                return True
            if v is None:
                unknown = True
        return None if unknown else False
    if "cmp" in cond:
        spec = cond["cmp"]
        left = eval_operand(spec["left"], row, captures, rows)
        right = eval_operand(spec["right"], row, captures, rows)
        return compare(spec["op"], left, right)
    raise ConditionError(f"unknown condition: {cond!r}")

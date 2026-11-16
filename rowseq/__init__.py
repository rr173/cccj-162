"""rowseq: a batch row-sequence query operator over SQLite.

Extracts "one entry, some checks, a final pass/exit"-style episodes from a
finite event table according to a pattern definition.  Pure batch data
preparation: no real-time watermarks, no alerting, no fixed device state
machine.  The core matching is implemented from scratch (iterative
backtracking VM); no MATCH_RECOGNIZE/CEP engine is used.
"""

from .pattern import (parse_pattern, validate_pattern, compile_pattern,
                      compute_live_vars, pattern_vars, nullable,
                      PatternError, Var, Seq, Alt, Rep)
from .conditions import (eval_cond, eval_operand, compare, validate_cond,
                         cond_refs, cond_fields, ConditionError)
from .engine import run_from, EngineMatch, EngineStats, StepLimitError
from .api import run_query, cross_check, QueryError, Match, QueryResult
from . import exhaustive

__version__ = "0.1.0"

__all__ = [
    "parse_pattern", "validate_pattern", "compile_pattern", "compute_live_vars",
    "pattern_vars", "nullable", "PatternError", "Var", "Seq", "Alt", "Rep",
    "eval_cond", "eval_operand", "compare", "validate_cond", "cond_refs",
    "cond_fields", "ConditionError",
    "run_from", "EngineMatch", "EngineStats", "StepLimitError",
    "run_query", "cross_check", "QueryError", "Match", "QueryResult",
    "exhaustive",
]

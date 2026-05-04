from __future__ import annotations
from typing import Any


class ConditionError(ValueError):
    """Raised when condition evaluation fails."""


def evaluate_condition(expression: str, context: dict[str, Any] | None = None) -> bool:
    if not expression or expression.strip() == "":
        return True
    try:
        from simpleeval import EvalWithCompoundTypes, NameNotDefined, InvalidExpression
    except ImportError:
        import warnings
        warnings.warn("simpleeval not installed. Condition evaluation disabled; all steps will run.", UserWarning, stacklevel=2)
        return True
    try:
        evaluator = EvalWithCompoundTypes(names=context or {})
        result = evaluator.eval(expression)
        return bool(result)
    except (NameNotDefined, InvalidExpression, ValueError, TypeError, SyntaxError) as e:
        raise ConditionError(f"Failed to evaluate condition {expression!r}: {e}") from e
    except Exception as e:
        import warnings
        warnings.warn(f"Unexpected condition evaluation error for {expression!r}: {e}. Defaulting to True.", UserWarning, stacklevel=2)
        return True


__all__ = ["ConditionError", "evaluate_condition"]

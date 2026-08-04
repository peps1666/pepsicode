from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

from pepsicode.hooks.models import HookContext

MAX_REGEX_LENGTH = 256
MAX_MATCH_VALUE_LENGTH = 4_000
_VALID_OPERATORS = {"eq", "in", "contains", "glob", "regex"}


class HookConditionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FieldCondition:
    field: str
    operator: str
    expected: Any
    regex: re.Pattern[str] | None = None

    def evaluate(self, context: HookContext) -> bool:
        actual = context.get_field(self.field)
        if self.operator == "eq":
            return actual == self.expected
        if self.operator == "in":
            return actual in self.expected
        actual_text = "" if actual is None else str(actual)[:MAX_MATCH_VALUE_LENGTH]
        if self.operator == "contains":
            return str(self.expected) in actual_text
        if self.operator == "glob":
            return fnmatch.fnmatch(actual_text, str(self.expected))
        if self.operator == "regex" and self.regex is not None:
            return bool(self.regex.search(actual_text))
        return False


@dataclass(frozen=True, slots=True)
class ConditionGroup:
    conditions: tuple[FieldCondition, ...] = ()

    def evaluate(self, context: HookContext) -> bool:
        return all(condition.evaluate(context) for condition in self.conditions)


def parse_conditions(raw: Any) -> ConditionGroup:
    if raw is None:
        return ConditionGroup()
    if not isinstance(raw, dict):
        raise HookConditionError("'when' must be an object")

    conditions: list[FieldCondition] = []
    for field, specification in raw.items():
        if not isinstance(field, str) or not field.strip():
            raise HookConditionError("condition fields must be non-empty strings")
        if isinstance(specification, dict):
            if len(specification) != 1:
                raise HookConditionError(f"condition '{field}' must contain exactly one operator")
            operator, expected = next(iter(specification.items()))
        elif isinstance(specification, list):
            operator, expected = "in", specification
        else:
            operator, expected = "eq", specification

        if operator not in _VALID_OPERATORS:
            raise HookConditionError(f"condition '{field}' has unsupported operator '{operator}'")
        if operator == "in" and not isinstance(expected, list):
            raise HookConditionError(f"condition '{field}.in' must be a list")

        compiled = None
        if operator == "regex":
            pattern = str(expected)
            if len(pattern) > MAX_REGEX_LENGTH:
                raise HookConditionError(f"condition '{field}.regex' exceeds {MAX_REGEX_LENGTH} characters")
            try:
                compiled = re.compile(pattern)
            except re.error as error:
                raise HookConditionError(f"condition '{field}.regex' is invalid: {error}") from error
        conditions.append(FieldCondition(field=field, operator=operator, expected=expected, regex=compiled))
    return ConditionGroup(tuple(conditions))

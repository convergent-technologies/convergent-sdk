"""The value rules behind ``init(require_span_attributes=)`` and
``init(reject_span_attributes=)``.

The caller maps attribute names to values. The require direction sends a span
only when every named attribute holds one of its allowed values. The reject
direction withholds a span when any named attribute holds one of its named
values. Reject decides first, so a pair named in both directions withholds.
``_processors`` reads each key from the stamped ``convergent.attributes.<key>``
mark first, then from the span's own attributes, then from the resource
attributes. A malformed filter is refused at startup,
because a typo read as "no filter" would send every trace: strict mode raises,
and the default logs the error and disables tracing. A span a require rule
cannot place is withheld: losing spans beats sending an excluded customer's.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, NamedTuple

from ._destinations import _type_name

logger = logging.getLogger("convergent.sdk")

#: What OpenTelemetry carries in an attribute. Conditions compare against these
#: only.
_ScalarValue = str | bool | int | float

_VALUE_TYPES: Final = (str, bool, int, float)


def _is_attribute_value(value: object) -> bool:
    """Compare types with ``is``, never membership. Membership compares with
    ``==``, and a metaclass ``__eq__`` can raise through it."""
    return any(type(value) is allowed for allowed in _VALUE_TYPES)


@dataclass(frozen=True, slots=True)
class Condition:
    """One attribute name compared against one set of values.

    ``values`` holds ``(type, value)`` pairs. The type is part of the key, so
    membership does the exact-type comparison on its own: ``(bool, True)`` and
    ``(int, 1)`` are different keys, and a condition written for ``1`` cannot
    match ``True``.
    """

    name: str
    values: frozenset[tuple[type, _ScalarValue]]


class Policy(NamedTuple):
    """Both directions of one configuration, either half optional."""

    require: frozenset[Condition] | None
    reject: frozenset[Condition] | None


def build(require_span_attributes: object, reject_span_attributes: object) -> Policy | None:
    """The policy ``require_span_attributes=`` and ``reject_span_attributes=``
    describe, or ``None`` to filter nothing.

    ``None`` for both sends every span, as an absent ``agents=`` does. A
    require key whose values are an empty list matches nothing, which is
    the answer ``agents=[]`` gives; an empty reject value list withholds
    nothing for that key. Frozensets, so two mappings that name the same
    conditions compare equal whatever their key order.

    A pair named in both directions is logged at ERROR here, because at
    runtime reject wins and the required value never sends.
    """
    if require_span_attributes is None and reject_span_attributes is None:
        return None
    required = (
        _parse_conditions(require_span_attributes, "require_span_attributes")
        if require_span_attributes is not None
        else None
    )
    rejected = (
        _parse_conditions(reject_span_attributes, "reject_span_attributes")
        if reject_span_attributes is not None
        else None
    )
    _report_contradictions(required, rejected)
    return Policy(required, rejected)


def decide(policy: Policy, sources: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the span these sources describe may be sent.

    Reject decides first: any rejected pair the span holds withholds it.
    Require decides next: a required key that is missing or holds an unlisted
    value withholds it. Otherwise the span is sent. A source that raises
    propagates: the span filter owns that guard.
    """
    if policy.reject is not None and any(
        _condition_matches(condition, sources) for condition in policy.reject
    ):
        return False
    if policy.require is not None and not all(
        _condition_matches(condition, sources) for condition in policy.require
    ):
        return False
    return True


def _condition_matches(condition: Condition, sources: Sequence[Mapping[str, Any]]) -> bool:
    for source in sources:
        if condition.name not in source:
            continue
        actual = source[condition.name]
        if not _is_attribute_value(actual):
            return False
        # Past the gate, the type and the value are exact builtins, safe to hash.
        return (type(actual), actual) in condition.values
    return False


def _report_contradictions(
    required: frozenset[Condition] | None, rejected: frozenset[Condition] | None
) -> None:
    """Log the pairs named in both directions, because reject wins at runtime.

    An ERROR rather than a raise: the configuration still works as documented,
    and each contradicting pair simply never sends. Once per process, the way
    the float warning below is: a repeat ``init()`` and a dropped processor
    both re-parse the same mapping.
    """
    if required is None or rejected is None:
        return
    rejected_values = {condition.name: condition.values for condition in rejected}
    contradictions = sorted(
        f"{condition.name}={value!r}"
        for condition in required
        for _, value in condition.values & rejected_values.get(condition.name, frozenset())
    )
    if contradictions:
        from . import _core  # deferred: _core imports this module

        _core._warn_once(
            "require_reject_contradiction",
            f"require_span_attributes= and reject_span_attributes= both name "
            f"{', '.join(contradictions)}. reject_span_attributes= wins, so a "
            "span holding such a pair is never sent. Remove the pair from one "
            "side.",
            level=logging.ERROR,
        )


def _parse_conditions(given: object, label: str) -> frozenset[Condition]:
    if not isinstance(given, Mapping):
        raise TypeError(f"{label}= takes a mapping of attribute to values, got {_type_name(given)}")
    parsed = frozenset(
        Condition(_parse_name(name, label), _parse_values(value, name, label))
        for name, value in given.items()
    )
    # Check the parsed result, never the caller's mapping. A truthy mapping that
    # iterates empty would build an empty set, and an empty set keeps every span.
    if not parsed:
        raise ValueError(
            f"{label}= is empty, so it says nothing. Leave it out to filter nothing, "
            "or pass an empty list of values to match nothing"
        )
    return parsed


def _parse_name(name: object, label: str) -> str:
    """One attribute name, verbatim, as the span records it."""
    if not isinstance(name, str):
        raise TypeError(f"{label}= has an attribute name of {_type_name(name)}; a name is a string")
    if not name or name.strip() != name:
        raise ValueError(
            f"{label}= has the attribute name {name!r}, which is empty or padded with "
            "spaces. No span records an attribute under that name"
        )
    return name


def _parse_values(value: object, name: object, label: str) -> frozenset[tuple[type, _ScalarValue]]:
    """The values for one condition, as ``(type, value)`` pairs.

    A bare scalar is one value. An empty list is legal and matches nothing,
    which is the answer ``agents=[]`` gives.
    """
    if value is None:
        raise ValueError(
            f"{label}= gives {name!r} no value to compare against; to match nothing, "
            "pass an empty list"
        )
    if isinstance(value, _VALUE_TYPES):
        given: tuple[object, ...] = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, str | bytes | Mapping):
        try:
            given = tuple(value)
        except Exception as error:
            raise TypeError(f"{label}= could not read the values for {name!r}") from error
    else:
        raise TypeError(
            f"{label}= compares {name!r} against {_type_name(value)}; use a "
            "string, number, bool, or a list of them"
        )

    validated: list[_ScalarValue] = []
    for item in given:
        if not isinstance(item, _VALUE_TYPES):
            raise TypeError(
                f"{label}= compares {name!r} against a {_type_name(item)}; "
                "an attribute holds a string, number or bool"
            )
        if not _is_attribute_value(item):
            # A subclass such as ``StrEnum`` passes the isinstance check above and
            # fails the exact-type comparison in ``_condition_matches``, so the
            # condition would build and never match. Not coerced: Enum overrides
            # __str__.
            base = next(t for t in _VALUE_TYPES if isinstance(item, t))
            raise TypeError(
                f"{label}= compares {name!r} against {_type_name(item)}, which subclasses "
                f"{base.__name__} rather than being one. A recorded "
                "attribute holds the plain type, so this condition could never match. "
                "Pass the underlying value instead -- '.value' for an enum member"
            )
        if type(item) is float:
            # Deferred: _core imports this module. Once per process: repeat
            # init() and a dropped processor both re-parse the same mapping.
            from . import _core

            _core._warn_once(
                f"{label}_float_condition",
                f"Convergent {label}= compares {name!r} against a float. Comparison "
                "is exact, so this condition may never match a recorded value.",
            )
        validated.append(item)
    return frozenset((type(item), item) for item in validated)


def as_mapping(
    conditions: frozenset[Condition] | None,
) -> dict[str, list[_ScalarValue]] | None:
    """The conditions as the mapping a caller could pass back in.

    Values sort by repr, so two processes running one configuration echo one
    text. ``None`` echoes an unconfigured direction.
    """
    if conditions is None:
        return None
    return {
        condition.name: sorted((value for _, value in condition.values), key=repr)
        for condition in conditions
    }


__all__ = ["Condition", "Policy", "as_mapping", "build", "decide"]

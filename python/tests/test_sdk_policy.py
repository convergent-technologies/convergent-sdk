"""The ``require_span_attributes=`` / ``reject_span_attributes=`` vocabulary as truth tables.

The evaluator holds no state, so every decision is answerable against plain
mappings: one table for what ``decide()`` answers, one for what ``build()``
refuses. Each row leads with why the row exists, and that string is the test id.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any

import pytest

from convergent import _core, _policy


@pytest.fixture(autouse=True)
def reset_once_per_process_warnings() -> Iterator[None]:
    """The contradiction ERROR fires once per process, so each test starts clean."""
    _core._warned.clear()
    yield
    _core._warned.clear()


def _built(
    require_span_attributes: Mapping[str, object] | None = None,
    reject_span_attributes: Mapping[str, object] | None = None,
) -> _policy.Policy:
    policy = _policy.build(require_span_attributes, reject_span_attributes)
    assert policy is not None
    return policy


def _decide(policy: _policy.Policy, *sources: Mapping[str, Any]) -> bool:
    return _policy.decide(policy, sources)


def test_no_filter_at_all_is_not_the_same_as_matching_nothing() -> None:
    """Collapsing the two would turn a caller who configured no filter into one
    who configured a total block."""
    assert _policy.build(None, None) is None


# --- decide(): policy x facts -> verdict -----------------------------------------
#
# Row shape: (why, require, reject, sources, expected). The sources are the
# span's attributes first, then the resource's, the order the filter reads them
# in. The non-scalar rows double as the never-raises guarantee: the filter
# reads any exception as a dropped span, so an exception out of decide() would
# be lost data rather than a reported problem.

_C = "customer.id"
_BOTH = {_C: ["A"], "env": ["prod"]}
_ONE = {_C: ["A"]}
_TWO_VALUES = {_C: ["A", "C"]}
_TWO_KEYS = {_C: ["A"], "env": ["dev"]}
_NO_DEV = {"env": ["dev"]}

_DECIDE_ROWS = [
    # require ANDs its keys; two values for one key are ORed.
    ("require ANDs its keys: both hold", _BOTH, None, ({_C: "A", "env": "prod"},), True),
    ("require ANDs its keys: env missing", _BOTH, None, ({_C: "A"},), False),
    ("require ANDs its keys: customer missing", _BOTH, None, ({"env": "prod"},), False),
    ("require ANDs its keys: one value wrong", _BOTH, None, ({_C: "B", "env": "prod"},), False),
    ("require ORs two values: first", _TWO_VALUES, None, ({_C: "A"},), True),
    ("require ORs two values: second", _TWO_VALUES, None, ({_C: "C"},), True),
    ("require ORs two values: unlisted", _TWO_VALUES, None, ({_C: "B"},), False),
    # Fail closed: a span carrying the key nowhere has not satisfied the rule.
    ("require: a missing key matches nothing", _ONE, None, ({"unrelated": "value"},), False),
    ("require: no sources at all match nothing", _ONE, None, (), False),
    # The answer agents=[] gives: a declaration was made and it admits no one.
    ("require: an empty value list matches nothing", {_C: []}, None, ({_C: "A"},), False),
    ("require: an empty value list matches no bare span", {_C: []}, None, ({},), False),
    ("require: a bare scalar is one value", {"env": "prod"}, None, ({"env": "prod"},), True),
    # reject withholds on a match; its keys are ORed, unlike require's.
    ("reject withholds a named pair", None, _ONE, ({_C: "A"},), False),
    ("reject passes an unlisted value", None, _ONE, ({_C: "B"},), True),
    ("reject ORs its keys: customer matches", None, _TWO_KEYS, ({_C: "A", "env": "prod"},), False),
    ("reject ORs its keys: env matches", None, _TWO_KEYS, ({_C: "B", "env": "dev"},), False),
    ("reject ORs its keys: one key alone withholds", None, _TWO_KEYS, ({_C: "A"},), False),
    ("reject ORs its keys: neither matches", None, _TWO_KEYS, ({_C: "B", "env": "prod"},), True),
    # A span the mapping never mentions passes.
    ("reject passes a span without the key", None, _ONE, ({"unrelated": "value"},), True),
    ("reject passes a bare span", None, _ONE, (), True),
    ("reject ORs two values: first", None, _TWO_VALUES, ({_C: "A"},), False),
    ("reject ORs two values: second", None, _TWO_VALUES, ({_C: "C"},), False),
    ("reject ORs two values: unlisted", None, _TWO_VALUES, ({_C: "B"},), True),
    ("reject: an empty value list withholds nothing", None, {_C: []}, ({_C: "A"},), True),
    # Both directions together must both keep, and reject decides first.
    ("together: both satisfied sends", _ONE, _NO_DEV, ({_C: "A", "env": "prod"},), True),
    ("together: the rejected pair withholds", _ONE, _NO_DEV, ({_C: "A", "env": "dev"},), False),
    ("together: the unmet require withholds", _ONE, _NO_DEV, ({_C: "B", "env": "prod"},), False),
    ("together: the missing require key withholds", _ONE, _NO_DEV, ({"env": "prod"},), False),
    ("reject wins when both name the same pair", _ONE, _ONE, ({_C: "A"},), False),
    # The lookup stops at the first source holding the key, so a later source
    # cannot overrule an earlier one, even with a matching value.
    ("require: the first holding source answers", _ONE, None, ({_C: "A"}, {_C: "B"}), True),
    ("require: a later source cannot overrule", _ONE, None, ({_C: "B"}, {_C: "A"}), False),
    ("require falls through a source without the key", _ONE, None, ({}, {_C: "A"}), True),
    ("reject: the first holding source answers", None, _ONE, ({_C: "A"}, {_C: "B"}), False),
    ("reject: a later source cannot overrule", None, _ONE, ({_C: "B"}, {_C: "A"}), True),
    ("reject falls through a source without the key", None, _ONE, ({}, {_C: "A"}), False),
    # A non-scalar value answers false without raising; a holding source with an
    # unreadable value still decides, or a later source could widen the filter.
    ("require: a mapping value is no match", {"t": ["x"]}, None, ({"t": {"n": "m"}},), False),
    ("require: a list value is no match", {"t": ["x"]}, None, ({"t": [1, 2]},), False),
    ("reject: a mapping value matches no pair", None, {"t": ["x"]}, ({"t": {"n": "m"}},), True),
    ("reject: a list value matches no pair", None, {"t": ["x"]}, ({"t": [1, 2]},), True),
    ("a holding source with a bad value decides", _ONE, None, ({_C: [1]}, {_C: "A"}), False),
]


@pytest.mark.parametrize(
    ("require", "reject", "sources", "expected"),
    [row[1:] for row in _DECIDE_ROWS],
    ids=[row[0] for row in _DECIDE_ROWS],
)
def test_decide(
    require: Mapping[str, object] | None,
    reject: Mapping[str, object] | None,
    sources: tuple[Mapping[str, Any], ...],
    expected: bool,
) -> None:
    assert _decide(_built(require, reject), *sources) is expected


@pytest.mark.parametrize(
    ("rule_value", "actual", "expected"),
    [
        (1, 1, True),
        (1, 1.0, False),
        (1, "1", False),
        (1, True, False),
        (True, 1, False),
        (True, True, True),
        ("Acme", "Acme", True),
        ("Acme", "acme", False),
    ],
)
def test_comparison_is_exact_by_type_and_case(
    rule_value: object, actual: object, expected: bool
) -> None:
    """bool subclasses int and True == 1, so the check is on type, not equality.
    A rule written for 1 matching an attribute holding True would be a leak."""
    assert (
        _decide(_built(require_span_attributes={"key": [rule_value]}), {"key": actual}) is expected
    )
    assert (
        _decide(_built(reject_span_attributes={"key": [rule_value]}), {"key": actual})
        is not expected
    )


# --- validation raises rather than degrading ------------------------------------

#: Each validation rule holds for both parameters, so every case runs twice.
_DIRECTIONS = ("require_span_attributes", "reject_span_attributes")


def _build_one(direction: str, mapping: object) -> _policy.Policy | None:
    if direction == "require_span_attributes":
        return _policy.build(mapping, None)
    return _policy.build(None, mapping)


class _Env(StrEnum):
    PROD = "prod"


class _TruthyButEmpty(dict[str, object]):
    """Truthy yet iterates empty. The emptiness check must run on the parsed
    result; run on the caller's mapping, this input would build an empty set,
    and an empty set keeps every span."""

    def __bool__(self) -> bool:
        return True


def _exploding_values() -> Iterator[str]:
    yield "a"
    raise RuntimeError("boom")


# Row shape: (why, given, exception, message-regex); "{label}" formats to the
# direction. A callable ``given`` is built per run, because a generator is
# consumed by its first run. bytes is load-bearing: unexcluded, b"ab" iterates
# to (97, 98) and silently builds a rule matching integer attributes. A
# subclass such as StrEnum would pass isinstance and fail the exact-type
# comparison, so the rule would build and never match; refused rather than
# coerced, since Enum overrides __str__.
_REFUSED_ROWS = [
    ("a bare string is not a mapping", "A", TypeError, "{label}= takes a mapping"),
    ("an object is not a mapping", object(), TypeError, "{label}= takes a mapping"),
    ("an empty mapping says nothing", {}, ValueError, "{label}= is empty"),
    ("an empty attribute name", {"": ["A"]}, ValueError, "empty or padded"),
    ("a blank attribute name", {" ": ["A"]}, ValueError, "empty or padded"),
    ("a left-padded attribute name", {" customer.id": ["A"]}, ValueError, "empty or padded"),
    ("a right-padded attribute name", {"customer.id ": ["A"]}, ValueError, "empty or padded"),
    ("a non-string attribute name", {7: ["A"]}, TypeError, "{label}= has an attribute name"),
    ("a null value points at the empty list", {_C: None}, ValueError, "pass an empty list"),
    ("a mapping is no attribute value", {_C: {"n": "m"}}, TypeError, "string, number, bool"),
    ("an object is no attribute value", {_C: object()}, TypeError, "string, number, bool"),
    ("bytes would iterate to integer rules", {_C: b"ab"}, TypeError, "string, number, bool"),
    (
        "unreadable values",
        lambda: {_C: _exploding_values()},
        TypeError,
        "could not read the values",
    ),
    ("a non-scalar inside a list", {_C: ["A", ["n"]]}, TypeError, "an attribute holds a string"),
    (
        "a subclass never matches, the .value fix is named",
        {"env": _Env.PROD},
        TypeError,
        r"\.value",
    ),
    ("a truthy mapping that iterates empty", _TruthyButEmpty(), ValueError, "says nothing"),
]


@pytest.mark.parametrize("direction", _DIRECTIONS)
@pytest.mark.parametrize(
    ("given", "exception", "match"),
    [row[1:] for row in _REFUSED_ROWS],
    ids=[str(row[0]) for row in _REFUSED_ROWS],
)
def test_build_refuses_what_no_span_could_satisfy(
    direction: str, given: Any, exception: type[Exception], match: str
) -> None:
    mapping = given() if callable(given) else given
    with pytest.raises(exception, match=match.format(label=direction)):
        _build_one(direction, mapping)


@pytest.mark.parametrize("direction", _DIRECTIONS)
def test_a_float_value_warns_because_exact_comparison_rarely_holds(
    direction: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Same never-matches failure as a subclass value. A float is a real attribute
    type, so this warns rather than refusing."""
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        _build_one(direction, {"score": [0.1]})

    assert any(
        "may never match" in record.message and f"{direction}=" in record.message
        for record in caplog.records
    )


# --- the contradiction report ----------------------------------------------------


def test_a_contradicting_pair_logs_an_error_at_build(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The configuration still builds and works as documented, so the problem is
    an ERROR line rather than a raise."""
    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        _built(
            require_span_attributes={"customer.id": ["A", "B"]},
            reject_span_attributes={"customer.id": ["B"], "env": ["dev"]},
        )

    errors = [r.message for r in caplog.records if "reject_span_attributes= wins" in r.message]
    assert len(errors) == 1
    assert "customer.id='B'" in errors[0]
    assert "'A'" not in errors[0]
    assert "env" not in errors[0]


def test_no_error_without_a_shared_pair(caplog: pytest.LogCaptureFixture) -> None:
    """The same key with disjoint values is the intended use, not a contradiction."""
    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        _built(
            require_span_attributes={"customer.id": ["A"]},
            reject_span_attributes={"customer.id": ["B"]},
        )

    assert not [r for r in caplog.records if "reject_span_attributes= wins" in r.message]


# --- the evaluator never raises on hostile objects ---------------------------------


def test_a_value_whose_class_raises_answers_false_without_raising() -> None:
    """decide reads type(actual), never actual.__class__ through isinstance."""

    class Hostile:
        @property
        def __class__(self) -> type:  # type: ignore[override]
            raise RuntimeError("boom")

    assert not _decide(_built(require_span_attributes={"env": ["prod"]}), {"env": Hostile()})


def test_a_value_whose_metaclass_raises_answers_false_without_raising() -> None:
    """Membership in _VALUE_TYPES compares classes with ==, which dispatches to a
    metaclass, so a metaclass defining __eq__ could raise out of decide and into
    tracer.start_span. The gate compares with `is`, which dispatches to nothing."""

    class Meta(type):
        def __eq__(cls, other: object) -> bool:
            raise RuntimeError("boom")

        def __hash__(cls) -> int:
            return 0

    class Exotic(metaclass=Meta):
        pass

    assert not _decide(_built(require_span_attributes={"env": ["prod"]}), {"env": Exotic()})

"""Validation and resolution of everything ``init()`` and the processor accept.

This is the raising half of the SDK. Startup is the one place a raise is
allowed, only for a problem that is local and deterministic, and only when the
caller turned strict mode on; with it off, ``_core`` logs the same error and
disables tracing instead. Nothing in this module touches a module global,
registers a hook, or builds a processor, which is what lets a raise leave the
process as it was. The one exception is :func:`_probe_file`, which creates the
spans file the caller asked for, because opening it is the check.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from opentelemetry.sdk.trace import TracerProvider

from . import _policy
from ._destinations import Console, Destination, File, _type_name

logger = logging.getLogger("convergent.sdk")

_DEFAULT_ENDPOINT = "https://ingest.convergent.dev"
_FLAG_ON = ("1", "true", "yes", "on")
_FLAG_OFF = ("0", "false", "no", "off")
# What POST /v1/deployments accepts in its agents field.
_DECLARED_AGENT_LIMIT = 256
_AGENT_NAME_LIMIT = 512


class _ApiKey(str):
    """The api key as a string whose repr never shows it.

    A raising ``init()`` propagates through frames whose locals hold the key, and
    a crash reporter that serializes locals (Sentry captures them by default)
    would carry the key off the machine. Those tools record ``repr``, so it is
    masked. ``str(key)`` and formatting still give the real value, which is what
    builds the Authorization header.

    Every ``str`` method returns a plain ``str``, so any transformation of a key
    (``strip``, concatenation) must re-wrap its result or the mask is lost.
    """

    def __repr__(self) -> str:
        return "'<convergent api key>'"


@dataclass(frozen=True)
class _Config:
    api_key: str | None
    endpoint: str | None
    release: str
    destinations: tuple[Destination, ...] = ()
    # None means no filtering; an empty tuple is a declaration matching no span.
    agents: tuple[str, ...] | None = None
    # Built from init(require_span_attributes=) and init(reject_span_attributes=)
    # at validation time, so a malformed value raises the way a malformed
    # agents= does. None means no filtering.
    policy: _policy.Policy | None = None
    # TracerProvider defines no __eq__, so this field compares by identity.
    tracer_provider: TracerProvider | None = None


def _validated_config(
    *,
    api_key: object,
    endpoint: object,
    release: object,
    agents: object,
    require_span_attributes: object,
    reject_span_attributes: object,
    destinations: object,
    tracer_provider: object,
    debug: object,
    strict: object,
) -> _Config:
    """The configuration ``init()`` was asked for, or a raise naming what was wrong.

    Types first and then values, so a caller who passed the wrong kind of thing
    hears that rather than a complaint about its contents. Nothing here touches a
    global, opens a descriptor, or makes a call, which is what lets ``init()``
    raise without leaving anything behind.
    """
    if not isinstance(debug, bool):
        raise TypeError(f"debug takes a bool, got {_type_name(debug)}")
    if not isinstance(strict, bool):
        raise TypeError(f"strict takes a bool, got {_type_name(strict)}")
    provider = _require_provider("tracer_provider", tracer_provider)
    given = _destinations_given(destinations)
    names = _agent_names(agents)
    policy = _resolved_policy(require_span_attributes, reject_span_attributes)

    _check_flag_env("CONVERGENT_DEBUG")
    _check_flag_env("CONVERGENT_STRICT")
    _check_traces_exporter_env()
    key, resolved_endpoint, resolved_release = _resolved_credentials(api_key, endpoint, release)
    if not key and not _any_destination_configured(given):
        raise ValueError(
            "Convergent has nothing to send spans to: pass api_key= or set "
            "CONVERGENT_API_KEY. To trace without credentials, pass "
            "destinations=[File(...)] or set CONVERGENT_SPANS_DIR, which writes "
            "spans to a file and sends nothing over the network. A process that "
            "wants no telemetry does not call init()."
        )
    return _Config(
        api_key=key or None,
        endpoint=resolved_endpoint or None,
        release=_required_release(resolved_release),
        destinations=_resolve_destinations(given, _clean(os.environ.get("CONVERGENT_SPANS_DIR"))),
        agents=names,
        policy=policy,
        tracer_provider=provider,
    )


def _processor_config(
    *,
    api_key: object,
    endpoint: object,
    release: object,
    agents: object,
    require_span_attributes: object,
    reject_span_attributes: object,
    tracer_provider: object,
    strict: object,
) -> _Config:
    """The configuration a ``ConvergentSpanProcessor`` was asked for, or a raise.

    ``init()``'s rules without ``destinations``, because the provider and
    everything else on it are the caller's. Credentials are required rather than
    optional for the same reason: a processor sends to Convergent and nowhere else,
    so there is no file for a keyless one to fall back on.
    """
    if not isinstance(strict, bool):
        raise TypeError(f"strict takes a bool, got {_type_name(strict)}")
    _check_flag_env("CONVERGENT_STRICT")
    provider = _require_provider("tracer_provider", tracer_provider)
    names = _agent_names(agents)
    policy = _resolved_policy(require_span_attributes, reject_span_attributes)
    key, resolved_endpoint, resolved_release = _resolved_credentials(api_key, endpoint, release)
    if not key:
        raise ValueError(
            "ConvergentSpanProcessor takes an api key: pass api_key= or set "
            "CONVERGENT_API_KEY. It sends to Convergent and nowhere else, so there is "
            "no destination to fall back on; init(destinations=[File(...)]) traces "
            "without credentials."
        )
    return _Config(
        api_key=key,
        endpoint=resolved_endpoint,
        release=_required_release(resolved_release),
        agents=names,
        policy=policy,
        tracer_provider=provider,
    )


def _resolved_credentials(
    api_key: object, endpoint: object, release: object
) -> tuple[str, str, str | None]:
    """Type-check and resolve the three credential arguments against the environment.

    The one resolution both entry points share, so a new variable lands in one
    place. The key is an :class:`_ApiKey` from the moment it is bound, so the
    endpoint check below can raise without a plain-str key in this frame.
    """
    _require_str("api_key", api_key)
    _require_str("endpoint", endpoint)
    _require_str("release", release)
    key = _ApiKey(_clean(api_key) or _clean(os.environ.get("CONVERGENT_API_KEY")))
    resolved_endpoint = _resolve_endpoint(endpoint, key)
    _check_endpoint(resolved_endpoint)
    resolved_release = _clean(release) or _clean(os.environ.get("CONVERGENT_RELEASE")) or None
    return key, resolved_endpoint, resolved_release


def _resolved_policy(
    require_span_attributes: object, reject_span_attributes: object
) -> _policy.Policy | None:
    """The filter policy, each direction resolved against the environment.

    The argument wins the way ``api_key`` wins over ``CONVERGENT_API_KEY``:
    the variable fills a direction in only when its argument is absent. The
    resolved mappings go through the same ``_policy.build`` an argument does,
    so a variable holding the wrong shape fails the way the argument would.
    """
    return _policy.build(
        _filter_env(require_span_attributes, "CONVERGENT_REQUIRE_SPAN_ATTRIBUTES"),
        _filter_env(reject_span_attributes, "CONVERGENT_REJECT_SPAN_ATTRIBUTES"),
    )


def _filter_env(argument: object, variable: str) -> object:
    if argument is not None:
        return argument
    value = _clean(os.environ.get(variable))
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError as error:
        raise ValueError(
            f"{variable} is not JSON; it takes a JSON object such as "
            '{"customer.id": ["acme"]}. The configured value is not echoed '
            "here in case it holds a secret by mistake"
        ) from error


def _required_release(resolved: str | None) -> str:
    """A release is required, because a trace that names no version cannot be
    compared with anything. Any string that names the version works."""
    if not resolved:
        raise ValueError(
            "Convergent needs a release: pass release= or set CONVERGENT_RELEASE. "
            "Any string that names the version of your code works: a git sha, a "
            "build id, a date. It is how one version's traces are compared with "
            "another's."
        )
    return resolved


def _masked_key[T](api_key: T) -> T:
    """``api_key`` for the caller to re-bind, masked when it is a string.

    Entry points rebind their argument through this before anything can raise,
    so their own frame never holds a plain-str key. A non-string passes through
    for the type check to reject.
    """
    if isinstance(api_key, str) and api_key:
        return cast(T, _ApiKey(api_key))
    return api_key


def _require_str(name: str, value: object) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} takes a string, got {_type_name(value)}")


def _require_provider(
    name: str, provider: object, *, required: bool = False
) -> TracerProvider | None:
    if provider is None and not required:
        return None
    if not isinstance(provider, TracerProvider):
        raise TypeError(
            f"{name} takes an opentelemetry.sdk.trace.TracerProvider, got {_type_name(provider)}"
        )
    return provider


def _destinations_given(destinations: object) -> tuple[Destination, ...]:
    """``destinations`` as a tuple, or a raise naming what is not a destination.

    Materialized here, so a generator is read once and the same entries reach
    :func:`_resolve_destinations` rather than arriving there already walked.
    """
    if isinstance(destinations, str | bytes) or not isinstance(destinations, Iterable):
        raise TypeError(
            "destinations takes a sequence of convergent.File and convergent.Console "
            f"values, got {_type_name(destinations)}"
        )
    try:
        given = tuple(destinations)
    except Exception as error:
        raise TypeError("destinations could not be iterated") from error
    for destination in given:
        if not isinstance(destination, File | Console):
            raise TypeError(
                "destinations takes convergent.File and convergent.Console values, got "
                f"{_type_name(destination)}"
            )
    return given


def _check_flag_env(name: str) -> None:
    """Reject an on/off variable whose value is neither.

    The configured value is not echoed, for the reason :func:`_check_endpoint`
    gives: a variable set from the wrong shell expansion can hold a secret, and
    the message reaches a log line.
    """
    value = _clean(os.environ.get(name))
    if value and value.lower() not in _FLAG_ON + _FLAG_OFF:
        raise ValueError(
            f"{name} is set to a value it does not take; it takes "
            f"{'/'.join(_FLAG_ON)} or {'/'.join(_FLAG_OFF)}"
        )


def _strict_enabled(strict: object) -> bool:
    """Whether a configuration that cannot work raises instead of disabling tracing.

    ``is True``, not truthiness, for the same reason as the debug flag: this also
    runs for values validation has not seen yet.
    """
    return strict is True or _clean(os.environ.get("CONVERGENT_STRICT")).lower() in _FLAG_ON


def _check_traces_exporter_env() -> None:
    value = _clean(os.environ.get("CONVERGENT_TRACES_EXPORTER"))
    if value and value.lower() != "console":
        raise ValueError(
            "CONVERGENT_TRACES_EXPORTER is set to a value it does not take; the only "
            "value it takes is 'console', which adds a console destination to the ones "
            "already configured"
        )


def _check_endpoint(endpoint: str) -> None:
    """Reject an endpoint nothing could be sent to.

    Empty is not an endpoint at all, which is the file-only process. Anything else
    has to be an address a request can be made to, because the same value
    registers the deployment and receives every span.

    The rejected value is not echoed. A key pasted into ``endpoint=`` or
    ``CONVERGENT_ENDPOINT`` by mistake is never a valid URL, so it would land in
    this message and in whatever captures it.
    """
    if not endpoint:
        return
    try:
        parsed = urlsplit(endpoint)
        usable = parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        usable = False
    if not usable:
        raise ValueError(
            "endpoint takes an http or https URL. The configured value is not "
            "echoed here in case it holds a secret by mistake; check endpoint= and "
            f"CONVERGENT_ENDPOINT. The default is {_DEFAULT_ENDPOINT}"
        )


def _any_destination_configured(given: Sequence[Destination]) -> bool:
    """Whether any source asked for a destination, which needs no credentials.

    Read from the argument and the environment rather than from the resolved
    tuple, so nothing has to resolve before the check that raises.
    """
    if given or _clean(os.environ.get("CONVERGENT_SPANS_DIR")):
        return True
    return _clean(os.environ.get("CONVERGENT_TRACES_EXPORTER")).lower() == "console"


def _agent_names(agents: object) -> tuple[str, ...] | None:
    """The names to filter spans on, or ``None`` to filter nothing.

    Any iterable of strings is accepted, because a tuple or a set of names is the
    same declaration a list is. A string is not an iterable of names here, because
    iterating one yields characters.

    ``agents=`` is a privacy control, so anything that is not names raises rather
    than being read as no declaration: doing that quietly would send every span the
    process records. The same name twice is one declaration and not a mistake.
    """
    if agents is None:
        return None
    if isinstance(agents, str | bytes) or not isinstance(agents, Iterable):
        raise TypeError(f"agents takes a list of agent names, got {_type_name(agents)}")
    try:
        given = tuple(agents)
    except Exception as error:
        raise TypeError("agents could not be iterated") from error

    names: list[str] = []
    for name in given:
        if not isinstance(name, str):
            raise TypeError(
                f"agents takes a list of agent names and one of them is a {_type_name(name)}"
            )
        stripped = name.strip()
        if not stripped:
            raise ValueError("agents takes agent names and one of them is empty")
        if len(stripped) > _AGENT_NAME_LIMIT:
            raise ValueError(
                f"agents takes names of at most {_AGENT_NAME_LIMIT} characters, which is "
                f"what registration accepts, and one of them is {len(stripped)} long"
            )
        names.append(stripped)

    declared = tuple(dict.fromkeys(names))
    if len(declared) > _DECLARED_AGENT_LIMIT:
        raise ValueError(
            f"agents takes at most {_DECLARED_AGENT_LIMIT} names, which is what "
            f"registration accepts, and got {len(declared)}"
        )
    return declared


def _target(destination: Destination) -> object:
    """The key two destinations are deduplicated on.

    For a ``File`` it is the path, so two ``File`` values naming one file collapse
    even when their modes differ. Two exporters on one file would append every span
    twice.
    """
    if isinstance(destination, File):
        return Path(destination.path) / destination.filename
    return destination


def _resolve_destinations(
    destinations: Sequence[Destination], spans_dir: str
) -> tuple[Destination, ...]:
    """Fold the shorthands into the explicit list, absolute paths resolved.

    ``CONVERGENT_TRACES_EXPORTER=console`` **adds** a console destination rather
    than replacing the others. The useful question here is "show me what I am
    sending", not "stop sending" -- a customer debugging a delivery problem needs
    both halves at once.

    Order matters here. Paths are made absolute *before* deduplication so that
    ``CONVERGENT_SPANS_DIR=/traces`` and ``File("/traces")`` collapse to one
    destination instead of two exporters appending every span twice to the same
    file.

    An explicit ``File`` gets the same absolute-path treatment
    ``CONVERGENT_SPANS_DIR`` gets, which is also what makes ``_Config`` equality
    behave -- ``File("/a")`` and ``File(Path("/a"))`` are the same destination, and
    comparing them unresolved made a re-``init()`` warn about a conflict that did
    not exist.
    """
    candidates: list[Destination] = []
    if spans_dir:
        candidates.append(File(os.path.abspath(spans_dir)))
    candidates.extend(
        _absolute(destination) if isinstance(destination, File) else destination
        for destination in destinations
    )
    if _clean(os.environ.get("CONVERGENT_TRACES_EXPORTER")).lower() == "console" and not any(
        isinstance(destination, Console) for destination in candidates
    ):
        candidates.append(Console())

    unique: dict[object, Destination] = {}
    for destination in candidates:
        unique.setdefault(_target(destination), destination)
    return tuple(unique.values())


def _absolute(destination: File) -> File:
    """``destination`` with its path resolved against the working directory.

    ``os.fspath`` runs the caller's own ``__fspath__``, which may raise anything
    at all: a cloud path reaching for a client it has no credentials for is the
    case that happens. The raise is normalized the way a caller iterable's is, so
    startup keeps the raise set it documents.
    """
    try:
        return replace(destination, path=os.path.abspath(os.fspath(destination.path)))
    except Exception as error:
        raise TypeError(
            "File(path=) takes a local filesystem path, as a string or an os.PathLike "
            "whose __fspath__ returns one, and this path could not be resolved to one"
        ) from error


def _probe_file(destination: File) -> None:
    """Prove one spans file can be opened, before ``init()`` changes anything.

    The same open the exporter performs -- create, append, ``O_NOFOLLOW``, the
    ``File``'s mode -- closed immediately. An unwritable directory raises its
    ``OSError`` here, so the exporter itself is only built once the process is
    committed. The directory chain and the file this creates stay: they are what
    the caller asked for.
    """
    target = Path(destination.path) / destination.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    os.close(
        os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            destination.mode,
        )
    )


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _resolve_endpoint(value: object, api_key: str) -> str:
    configured = _clean(value) or _clean(os.environ.get("CONVERGENT_ENDPOINT"))
    return configured or (_DEFAULT_ENDPOINT if api_key else "")

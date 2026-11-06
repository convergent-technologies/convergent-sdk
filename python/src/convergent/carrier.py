"""Inject and extract trace context, with the Convergent entry, for a carrier you pass.

Named ``carrier`` so it never shadows ``opentelemetry.context``.
"""

from __future__ import annotations

from opentelemetry.context import Context
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    default_getter,
    default_setter,
)

from . import _propagation


def inject(
    carrier: CarrierT,
    context: Context | None = None,
    setter: Setter[CarrierT] = default_setter,
    *,
    session: bool = False,
) -> None:
    """Inject the active span and its SDK trajectory into ``carrier``.

    The session id goes too only with ``session=True``, for a carrier the
    caller knows stays inside their own services, such as a job row.
    """
    with _propagation._sending_session(session):
        _propagation.public_propagator().inject(carrier, context, setter)


def extract(
    carrier: CarrierT,
    context: Context | None = None,
    getter: Getter[CarrierT] = default_getter,
) -> Context:
    """Extract a carrier, including a validated remote SDK trajectory.

    Args:
        carrier: The object to read from, such as a dict of headers.
        context: The context to extract into; the current one by default.
        getter: Reads one header from ``carrier``.

    Returns:
        A context with the remote span as parent. When the carrier holds a
        valid Convergent entry naming that span, agents started in it are filed
        under the sender's agent.
    """
    return _propagation.public_propagator().extract(carrier, context, getter)

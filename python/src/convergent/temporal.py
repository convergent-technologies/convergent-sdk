"""Optional Temporal tracing integration for the Convergent SDK.

temporalio's own tracing, with one change: each workflow run, including a
continued-as-new, cron or child run, makes its caller's Convergent entry fresh,
so the first agent inside the run is the root of its own trajectory, never a
child of the caller's. An agent in one of the run's activities nests under the
workflow's agent that handed off to it. Under temporalio's own interceptor every
later run starts from the first run's caller: each cron run gets the first run's
start header, and continue-as-new writes the run's current context, which is
still the caller's, into the next run's header. Reusing the caller's entry
would file weeks of runs under one caller's trajectory.
"""

from __future__ import annotations

from typing import Any, override

from opentelemetry.context import Context
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)

try:
    from temporalio.contrib.opentelemetry import (
        TracingInterceptor as _TracingInterceptor,
    )
    from temporalio.contrib.opentelemetry import (
        TracingWorkflowInboundInterceptor as _TracingWorkflowInboundInterceptor,
    )
    from temporalio.worker import (
        WorkflowInboundInterceptor,
        WorkflowInterceptorClassInput,
    )
except ImportError as error:
    raise ImportError(
        'convergent.temporal needs Temporal: pip install "convergent-sdk[temporal]"'
    ) from error

# Everything the interceptors call is bound here, outside Temporal's workflow
# sandbox. The sandbox swaps the module table while a workflow task runs, so an
# import or a lazy ``convergent.<name>`` lookup made then loads a second SDK.
from . import _core, _processors, _propagation


def _fall_back(error: Exception) -> None:
    """Warn once that making a run's entry fresh raised.

    A workflow task must never fail because of the SDK's tracing, so the run
    then keeps temporalio's own context with the caller's entry cut out.
    """
    _core._warn_once(
        "temporal_fresh_entry",
        "convergent.temporal: could not mark this Temporal run's Convergent entry fresh "
        f"({type(error).__name__}), so the run keeps temporalio's trace context "
        "without the caller's Convergent entry",
    )


class _Propagator(TextMapPropagator):
    """temporalio's own propagator wrapped with the Convergent entry, sending the
    session id only when asked to.

    Not the process-global one: this interceptor replaces temporalio's, so its
    headers carry what temporalio's carry (W3C trace context and baggage) whatever
    ``OTEL_PROPAGATORS`` says.
    """

    def __init__(self, temporal: TextMapPropagator, send_session: bool) -> None:
        self._inner = _propagation.ConvergentPropagator(temporal)
        self._send_session = send_session

    def inject(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        setter: Setter[CarrierT] = default_setter,
    ) -> None:
        with _propagation._sending_session(self._send_session):
            self._inner.inject(carrier, context, setter)

    def extract(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        getter: Getter[CarrierT] = default_getter,
    ) -> Context:
        return self._inner.extract(carrier, context, getter)

    @property
    def fields(self) -> set[str]:
        return self._inner.fields


def _fresh_carrier(
    propagator: TextMapPropagator, context: Context, received: _processors._Received | None
) -> dict[str, Any]:
    """``context`` as a carrier whose Convergent entry is fresh for its current span.

    Agents in the run start new trajectories. They keep the caller's session only
    when both interceptors have send_session=True: the caller's, or the start header
    does not carry it, and this worker's, or the inject leaves it out of the carrier.
    """
    session_id = received.session_id if received is not None else None
    carrier: dict[str, Any] = {}
    propagator.inject(carrier, _processors.with_received(context, None, session_id))
    return carrier


class TracingInterceptor(_TracingInterceptor):
    """Trace Temporal workflows and activities, each run's agents starting new trajectories.

    The session id stays behind unless ``send_session=True``, which also stores
    it in Temporal's workflow history. Other arguments pass through to
    temporalio's ``TracingInterceptor``.
    """

    def __init__(self, *args: Any, send_session: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.text_map_propagator = _Propagator(self.text_map_propagator, send_session)

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[_TracingWorkflowInboundInterceptor]:
        super().workflow_interceptor_class(input)
        if not hasattr(_TracingWorkflowInboundInterceptor, "_load_workflow_context_carrier"):
            _core._warn_once(
                "temporal_override_missing",
                "convergent.temporal: this temporalio has no "
                "TracingWorkflowInboundInterceptor._load_workflow_context_carrier, so "
                "cron, continue-as-new and child runs keep their caller's Convergent "
                "entry and their agents nest under the caller's trajectory",
            )
        # Built outside the sandbox and run inside it, so it holds only objects
        # resolved at import.
        workflow_propagator = self.text_map_propagator

        class _WorkflowInboundInterceptor(_TracingWorkflowInboundInterceptor):
            def __init__(self, next: WorkflowInboundInterceptor) -> None:
                super().__init__(next)
                self.text_map_propagator = workflow_propagator
                self._made_fresh = False

            @override
            def _load_workflow_context_carrier(self) -> dict[str, Any] | None:
                """temporalio's start-header carrier, its Convergent entry made fresh.

                temporalio copies this context into a cron or continue-as-new run,
                and the caller's entry names the StartWorkflow span as sender, so
                kept as is it would file every later run's agents in the caller's
                trajectory. Only the entry changes; the RunWorkflow span stays
                under StartWorkflow. Made once per run: temporalio's own method
                rereads the header whenever the cached carrier is empty. If making
                it raises, the run keeps temporalio's carrier, so its RunWorkflow
                span and the caller's baggage, minus only the ``convergent=``
                member.
                """
                if not self._made_fresh:
                    self._made_fresh = True
                    carrier = None
                    try:
                        carrier = super()._load_workflow_context_carrier()
                        if carrier is not None:
                            start = self.text_map_propagator.extract(carrier)
                            # A held entry's session goes on only once it passed.
                            received = _processors._checked(_processors._remote_received(start))
                            carrier = _fresh_carrier(self.text_map_propagator, start, received)
                    except Exception as error:  # noqa: BLE001 - see _fall_back
                        _fall_back(error)
                        _propagation._cut_carrier_entry(carrier)
                    self._workflow_context_carrier = carrier
                return self._workflow_context_carrier

        return _WorkflowInboundInterceptor


__all__ = ["TracingInterceptor"]

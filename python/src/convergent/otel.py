"""Convergent as one OpenTelemetry span processor, for a provider you own."""

from ._otel import ConvergentSpanProcessor, install

__all__ = ["ConvergentSpanProcessor", "install"]

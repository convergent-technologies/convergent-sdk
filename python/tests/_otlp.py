"""Read a spans file back the way a receiver would.

Upstream these tests call ``convergent.interop.sources.otel.records_from_line``,
the real receiver-side reader. That reader is not part of the SDK distribution and
pulling it in would drag a much larger tree behind it, so this is a
stand-in: it flattens one OTLP/JSON export request into one record per span, which
is the only part of the reader those tests use.

It deliberately proves nothing about the format itself. ``test_file_export.py``'s
``test_each_line_is_one_span_of_spec_shaped_otlp_json`` asserts the wire shape
straight off the raw JSON, and that is the test that holds the contract.
"""

from __future__ import annotations

import json
from typing import Any


def _value(any_value: dict[str, Any]) -> Any:
    """One OTLP ``AnyValue`` as the Python value it wraps."""
    for key, value in any_value.items():
        if key == "arrayValue":
            return [_value(item) for item in value.get("values", [])]
        if key == "kvlistValue":
            return _attributes(value.get("values", []))
        if key == "intValue":
            return int(value)
        return value
    return None


def _attributes(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    return {pair["key"]: _value(pair.get("value") or {}) for pair in pairs}


def records_from_line(record: Any) -> list[dict[str, Any]]:
    """One line of a spans file, as zero or more span records."""
    if isinstance(record, str):
        record = json.loads(record)
    if not isinstance(record, dict) or "resourceSpans" not in record:
        return [record]
    records = []
    for resource_spans in record["resourceSpans"]:
        resource = _attributes(resource_spans.get("resource", {}).get("attributes", []))
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                records.append(
                    {
                        **span,
                        "attributes": _attributes(span.get("attributes", [])),
                        "resource": resource,
                        "scope": scope_spans.get("scope", {}),
                    }
                )
    return records

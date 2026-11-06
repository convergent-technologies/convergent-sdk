"""The OTLP receiver routes and refusal reason the SDK uses.

Generated from the Convergent OTLP receiver's OpenAPI spec
(schemas/openapi/otlp-receiver.json in the Convergent repository). Do not edit.
"""

#: OTLP spans.
TRACES_PATH = "/v1/traces"

#: One captured file, by content hash.
ARTIFACT_PATH = "/v1/artifacts/{digest}"

#: Diagnoses an installation.
CHECK_PATH = "/v1/check"

#: The header that carries a refusal reason on answers with no body.
REASON_HEADER = "X-Convergent-Reason"

#: The one reason that pauses uploads; any other 403 or 404 is a proxy's.
UPLOADS_OFF_REASON = "artifact_uploads_disabled"

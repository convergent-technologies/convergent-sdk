"""Telemetry keys the Convergent SDK writes.

Generated from the Convergent telemetry registry. Do not edit by hand.
"""

# sha256 of the registry file these constants were generated from.
# pragma: allowlist nextline secret
REGISTRY_SHA256 = "2114924fbc651632c077d232638cb35deb4964eaeaa8e3316b71701b15338b24"

#: Logical operation name to the gen_ai.operation.name value the SDK writes.
OPERATIONS: dict[str, str] = {
    "agent_run": "invoke_agent",
    "model_call": "chat",
    "tool_call": "execute_tool",
    "retrieval": "retrieval",
    "embeddings": "embeddings",
    "workflow": "invoke_workflow",
    "agent_create": "create_agent",
    "text_completion": "text_completion",
    "generate_content": "generate_content",
}

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_AGENT_VERSION = "gen_ai.agent.version"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_TYPE = "gen_ai.tool.type"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result"
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
CONVERGENT_CONTENT_SOURCE = "convergent.content.source"
CONVERGENT_SESSION_ID = "convergent.session.id"
CONVERGENT_ARTIFACTS_INPUT = "convergent.artifacts.input"
CONVERGENT_ARTIFACTS_OUTPUT = "convergent.artifacts.output"
CONVERGENT_ARTIFACTS_DROPPED = "convergent.artifacts.dropped"
CONVERGENT_AGENT_TOOLS = "convergent.agent.tools"
CONVERGENT_AGENT_SYSTEM_INSTRUCTIONS = "convergent.agent.system_instructions"
CONVERGENT_INVOCATION_SPAN_ID = "convergent.invocation.span_id"
CONVERGENT_INVOCATION_START_TIME_UNIX_MICROS = "convergent.invocation.start_time_unix_micros"
CONVERGENT_ROOT_INVOCATION_SPAN_ID = "convergent.root_invocation.span_id"
CONVERGENT_ROOT_INVOCATION_START_TIME_UNIX_MICROS = (
    "convergent.root_invocation.start_time_unix_micros"
)
CONVERGENT_PARENT_INVOCATION_SPAN_ID = "convergent.parent_invocation.span_id"
CONVERGENT_INVOCATION_STAMP_FAILED = "convergent.invocation.stamp_failed"
CONVERGENT_SDK_VERSION = "convergent.sdk.version"
CONVERGENT_ATTRIBUTES_PREFIX = "convergent.attributes."

#: Every key above, for code that checks each one.
KEYS: tuple[str, ...] = (
    GEN_AI_OPERATION_NAME,
    GEN_AI_AGENT_NAME,
    GEN_AI_AGENT_VERSION,
    GEN_AI_TOOL_NAME,
    GEN_AI_TOOL_TYPE,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_CALL_ARGUMENTS,
    GEN_AI_TOOL_CALL_RESULT,
    GEN_AI_INPUT_MESSAGES,
    GEN_AI_OUTPUT_MESSAGES,
    GEN_AI_REQUEST_MODEL,
    CONVERGENT_CONTENT_SOURCE,
    CONVERGENT_SESSION_ID,
    CONVERGENT_ARTIFACTS_INPUT,
    CONVERGENT_ARTIFACTS_OUTPUT,
    CONVERGENT_ARTIFACTS_DROPPED,
    CONVERGENT_AGENT_TOOLS,
    CONVERGENT_AGENT_SYSTEM_INSTRUCTIONS,
    CONVERGENT_INVOCATION_SPAN_ID,
    CONVERGENT_INVOCATION_START_TIME_UNIX_MICROS,
    CONVERGENT_ROOT_INVOCATION_SPAN_ID,
    CONVERGENT_ROOT_INVOCATION_START_TIME_UNIX_MICROS,
    CONVERGENT_PARENT_INVOCATION_SPAN_ID,
    CONVERGENT_INVOCATION_STAMP_FAILED,
    CONVERGENT_SDK_VERSION,
)

#: Every key family's prefix above, for code that checks each one.
KEY_PREFIXES: tuple[str, ...] = (CONVERGENT_ATTRIBUTES_PREFIX,)

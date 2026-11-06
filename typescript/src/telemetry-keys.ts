// Telemetry keys the Convergent SDK writes.
//
// Generated from the Convergent telemetry registry. Do not edit by hand.

// sha256 of the registry file these constants were generated from.
// pragma: allowlist nextline secret
export const REGISTRY_SHA256 = "2114924fbc651632c077d232638cb35deb4964eaeaa8e3316b71701b15338b24";

/** Logical operation name to the gen_ai.operation.name value the SDK writes. */
export const OPERATIONS = {
  agent_run: "invoke_agent",
  model_call: "chat",
  tool_call: "execute_tool",
  retrieval: "retrieval",
  embeddings: "embeddings",
  workflow: "invoke_workflow",
  agent_create: "create_agent",
  text_completion: "text_completion",
  generate_content: "generate_content",
} as const;

export const GEN_AI_OPERATION_NAME = "gen_ai.operation.name";
export const GEN_AI_AGENT_NAME = "gen_ai.agent.name";
export const GEN_AI_AGENT_VERSION = "gen_ai.agent.version";
export const GEN_AI_TOOL_NAME = "gen_ai.tool.name";
export const GEN_AI_TOOL_TYPE = "gen_ai.tool.type";
export const GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id";
export const GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments";
export const GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result";
export const GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages";
export const GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages";
export const GEN_AI_REQUEST_MODEL = "gen_ai.request.model";
export const CONVERGENT_CONTENT_SOURCE = "convergent.content.source";
export const CONVERGENT_SESSION_ID = "convergent.session.id";
export const CONVERGENT_ARTIFACTS_INPUT = "convergent.artifacts.input";
export const CONVERGENT_ARTIFACTS_OUTPUT = "convergent.artifacts.output";
export const CONVERGENT_ARTIFACTS_DROPPED = "convergent.artifacts.dropped";
export const CONVERGENT_AGENT_TOOLS = "convergent.agent.tools";
export const CONVERGENT_AGENT_SYSTEM_INSTRUCTIONS = "convergent.agent.system_instructions";
export const CONVERGENT_INVOCATION_SPAN_ID = "convergent.invocation.span_id";
export const CONVERGENT_INVOCATION_START_TIME_UNIX_MICROS =
  "convergent.invocation.start_time_unix_micros";
export const CONVERGENT_ROOT_INVOCATION_SPAN_ID = "convergent.root_invocation.span_id";
export const CONVERGENT_ROOT_INVOCATION_START_TIME_UNIX_MICROS =
  "convergent.root_invocation.start_time_unix_micros";
export const CONVERGENT_PARENT_INVOCATION_SPAN_ID = "convergent.parent_invocation.span_id";
export const CONVERGENT_INVOCATION_STAMP_FAILED = "convergent.invocation.stamp_failed";
export const CONVERGENT_SDK_VERSION = "convergent.sdk.version";
export const CONVERGENT_ATTRIBUTES_PREFIX = "convergent.attributes.";

/** Every key above, for code that checks each one. */
export const KEYS = [
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
] as const;

/** Every key family's prefix above, for code that checks each one. */
export const KEY_PREFIXES = [CONVERGENT_ATTRIBUTES_PREFIX] as const;

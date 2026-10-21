// Agent and attribute filters for Convergent's own export path. Other exporters on the provider
// still receive every span, so none of this runs as a sampler.
import { createContextKey, type Attributes, type Context } from "@opentelemetry/api";
import type { ReadableSpan, Span } from "@opentelemetry/sdk-trace-base";
import { reservedAttributeKeys } from "./attributes.js";
import { reportOnce } from "./diagnostics.js";
import { typeName } from "./flags.js";

export type FilterValue = string | number | boolean;
export type SpanAttributeRules = Readonly<Record<string, FilterValue | readonly FilterValue[]>>;
export type ContextAttributes = Readonly<Record<string, FilterValue>>;

type Rules = ReadonlyMap<string, ReadonlySet<FilterValue>>;

export interface Filters {
  agents?: ReadonlySet<string> | undefined;
  require?: Rules | undefined;
  reject?: Rules | undefined;
}

export interface FilterOptions {
  agents?: readonly string[];
  requireSpanAttributes?: SpanAttributeRules;
  rejectSpanAttributes?: SpanAttributeRules;
}

// Request marks: pairs, and whether any input failed to resolve.
export interface Marks {
  pairs: Record<string, FilterValue>;
  unresolved: boolean;
}

// What the processor saves for one span when it starts.
export interface SpanMetadata extends Marks {
  // The GenAI operation the span declared at start.
  operation?: string;
  sessionId?: string;
  keep: boolean;
  // convergent.attributes.* values the span already held when it started.
  startMarks?: Attributes;
}

export interface FilterDescription {
  agents: readonly string[];
  requireSpanAttributes: Readonly<Record<string, readonly FilterValue[]>> | null;
  rejectSpanAttributes: Readonly<Record<string, readonly FilterValue[]>> | null;
}

// The longest agent list and agent name the SDKs accept.
const agentNameLimit = 512;
const agentLimit = 256;
// Exact names or dotted children, so a "litellm_proxy" tracer is not litellm.
const modelScopes = ["convergent.sdk", "pydantic-ai", "openinference", "litellm"];
const sessionKeys = new Set(["gen_ai.conversation.id", "session.id", "convergent.session.id"]);
const markPrefix = "convergent.attributes.";

const isScalar = (value: unknown): value is FilterValue =>
  typeof value === "string" || typeof value === "number" || typeof value === "boolean";

// Plain objects only. A Promise, Map, or class instance lists no entries and
// would otherwise read as a resolved, empty mapping.
const isMapping = (value: unknown): value is Record<string, unknown> => {
  if (value === null || typeof value !== "object") return false;
  const prototype: unknown = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
};

// The checks and messages are shared by the SDKs: a
// TypeError for the wrong kind of value and an Error for a value that cannot work.
function agentNames(given: unknown): ReadonlySet<string> | undefined {
  if (given === undefined) return undefined;
  if (!Array.isArray(given)) {
    throw new TypeError(`agents takes a list of agent names, got ${typeName(given)}`);
  }
  const names = new Set<string>();
  for (const name of given) {
    if (typeof name !== "string") {
      throw new TypeError(
        `agents takes a list of agent names and one of them is a ${typeName(name)}`,
      );
    }
    const trimmed = name.trim();
    if (!trimmed) throw new Error("agents takes agent names and one of them is empty");
    const length = Array.from(trimmed).length;
    if (length > agentNameLimit) {
      throw new Error(
        `agents takes names of at most ${agentNameLimit} characters, which is what registration accepts, and one of them is ${length} long`,
      );
    }
    names.add(trimmed);
  }
  if (names.size > agentLimit) {
    throw new Error(
      `agents takes at most ${agentLimit} names, which is what registration accepts, and got ${names.size}`,
    );
  }
  return names;
}

function fromEnvironment(argument: unknown, variable: string): unknown {
  if (argument !== undefined) return argument;
  const raw = process.env[variable]?.trim();
  if (!raw) return undefined;
  try {
    return JSON.parse(raw);
  } catch {
    throw new Error(
      `${variable} is not JSON. It takes a JSON object such as {"customer.id": ["acme"]}. The configured value is not echoed here in case it holds a secret by mistake`,
    );
  }
}

function values(value: unknown, name: string, label: string): FilterValue[] {
  if (value === null || value === undefined) {
    throw new Error(
      `${label} gives ${JSON.stringify(name)} no value to compare against. To match nothing, pass an empty list`,
    );
  }
  if (isScalar(value)) return [value];
  if (!Array.isArray(value)) {
    throw new TypeError(
      `${label} compares ${JSON.stringify(name)} against ${typeName(value)}. Use a string, number, boolean, or a list of them`,
    );
  }
  for (const item of value) {
    if (!isScalar(item)) {
      throw new TypeError(
        `${label} compares ${JSON.stringify(name)} against a ${typeName(item)}. An attribute holds a string, number, or boolean`,
      );
    }
  }
  return value as FilterValue[];
}

function rules(given: unknown, label: string): Rules | undefined {
  if (given === undefined) return undefined;
  if (!isMapping(given)) {
    throw new TypeError(`${label} takes a mapping of attribute to values, got ${typeName(given)}`);
  }
  const parsed = new Map<string, ReadonlySet<FilterValue>>();
  for (const [name, value] of Object.entries(given)) {
    if (!name || name.trim() !== name) {
      throw new Error(
        `${label} has the attribute name ${JSON.stringify(name)}, which is empty or padded with spaces. No span records an attribute under that name`,
      );
    }
    parsed.set(name, new Set(values(value, name, label)));
  }
  // An empty rule set would keep every span, so it is refused like a typo.
  if (parsed.size === 0) {
    throw new Error(
      `${label} is empty, so it says nothing. Leave it out to filter nothing, or pass an empty list of values to match nothing`,
    );
  }
  return parsed;
}

function reportContradictions(filters: Filters): void {
  const both = [...(filters.require ?? [])].flatMap(([name, values]) =>
    [...values]
      .filter((value) => filters.reject?.get(name)?.has(value))
      .map((value) => `${name}=${JSON.stringify(value)}`),
  );
  if (both.length) {
    reportOnce(
      "require_reject_contradiction",
      `requireSpanAttributes and rejectSpanAttributes both name ${both.sort().join(", ")}. rejectSpanAttributes wins, so a span holding such a pair is never sent. Remove the pair from one side.`,
    );
  }
}

/**
 * The filters to apply. Throws when the options cannot work, so setup disables
 * Convergent export, or throws under strict mode, instead of sending unfiltered
 * spans. Explicit options win over the CONVERGENT_*_SPAN_ATTRIBUTES JSON variables.
 */
export function parseFilters(options: FilterOptions): Filters {
  const filters: Filters = {
    agents: agentNames(options.agents),
    require: rules(
      fromEnvironment(options.requireSpanAttributes, "CONVERGENT_REQUIRE_SPAN_ATTRIBUTES"),
      "requireSpanAttributes",
    ),
    reject: rules(
      fromEnvironment(options.rejectSpanAttributes, "CONVERGENT_REJECT_SPAN_ATTRIBUTES"),
      "rejectSpanAttributes",
    ),
  };
  reportContradictions(filters);
  return filters;
}

function compare(left: string, right: string): number {
  if (left < right) return -1;
  return left > right ? 1 : 0;
}

const byText = (a: FilterValue, b: FilterValue): number =>
  compare(JSON.stringify(a), JSON.stringify(b));

export function describeFilters({ agents, require, reject }: Filters): FilterDescription {
  // Sorted, so two processes with one configuration print one text.
  const echo = (given: Rules) =>
    Object.fromEntries(
      [...given]
        .sort(([a], [b]) => compare(a, b))
        .map(([name, values]) => [name, [...values].sort(byText)]),
    );
  return {
    agents: agents ? [...agents] : [],
    requireSpanAttributes: require ? echo(require) : null,
    rejectSpanAttributes: reject ? echo(reject) : null,
  };
}

function matches(name: string, values: ReadonlySet<FilterValue>, sources: readonly Attributes[]) {
  for (const source of sources) {
    if (!Object.hasOwn(source, name)) continue;
    // The first source holding the key answers. Set membership compares type
    // and value, so true never matches 1. JavaScript numbers make 1 match 1.0.
    const actual = source[name];
    return isScalar(actual) && values.has(actual);
  }
  return false;
}

// Reject runs first, then every require key must match. A span whose request
// marks could not be resolved is withheld under any attribute rule.
export function passesAttributeRules(
  { require, reject }: Filters,
  span: ReadableSpan,
  metadata: SpanMetadata | undefined,
): boolean {
  if (!require && !reject) return true;
  if (metadata?.unresolved) return false;
  const sources = [spanMarks(span.attributes, metadata), span.attributes, span.resource.attributes];
  if (reject && [...reject].some(([name, values]) => matches(name, values, sources))) return false;
  return !require || [...require].every(([name, values]) => matches(name, values, sources));
}

function isModelWork(span: Span): boolean {
  if (Object.keys(span.attributes).some((key) => key.startsWith("gen_ai."))) return true;
  const scope = span.instrumentationScope.name;
  return modelScopes.some((name) => scope === name || scope.startsWith(`${name}.`));
}

// The agent rule, decided once when the span starts. A name added later does not count.
export function agentDecision(
  agents: ReadonlySet<string> | undefined,
  span: Span,
  parent: SpanMetadata | undefined,
): boolean {
  if (!agents) return true;
  if (agents.size === 0) return false;
  const name = span.attributes["gen_ai.agent.name"];
  if (typeof name === "string" && name) return agents.has(name);
  const parentContext = span.parentSpanContext;
  if (!parentContext) return false;
  // A remote parent's decision is unknown here, so model work from another process is kept.
  if (parentContext.isRemote) return isModelWork(span);
  return parent?.keep ?? false;
}

export function markAttributes(attributes: Attributes): Attributes {
  return Object.fromEntries(
    Object.entries(attributes).filter(([key]) => key.startsWith(markPrefix)),
  );
}

// The span's marks, by bare key. A convergent.attributes.<key> attribute on the
// span counts as a mark. A value the span held at start yields to the request
// mark, and a value written after start replaces it.
export function spanMarks(attributes: Attributes, metadata: SpanMetadata | undefined): Attributes {
  const marks: Attributes = { ...metadata?.pairs };
  for (const [key, value] of Object.entries(markAttributes(attributes))) {
    const name = key.slice(markPrefix.length);
    if (!(name in marks) || metadata?.startMarks?.[key] !== value) marks[name] = value;
  }
  return marks;
}

export function exportMarks(marks: Attributes): Attributes {
  return Object.fromEntries(Object.entries(marks).map(([key, value]) => [markPrefix + key, value]));
}

// Request scope: the marks an SDK span hands to the spans started inside it.
// It stays in this process and never enters baggage. Children read it from
// the context as well as from their parent span's metadata, so marks still
// arrive when the parent span is not recorded or a child starts as a root.
const scopeKey = createContextKey("convergent.context.attributes");
const none: Marks = { pairs: {}, unresolved: false };
const unresolvable: Marks = { pairs: {}, unresolved: true };

function requestScope(parentContext: Context): Marks | undefined {
  return parentContext.getValue(scopeKey) as Marks | undefined;
}

// A new scope for one SDK span: the inherited pairs with this call's overrides.
// An unresolved ancestor stays unresolved.
export function withRequestScope(
  parentContext: Context,
  update: Marks,
): { context: Context; scope: Marks } {
  const inherited = requestScope(parentContext);
  const scope: Marks = {
    pairs: { ...inherited?.pairs, ...update.pairs },
    unresolved: (inherited?.unresolved ?? false) || update.unresolved,
  };
  return { context: parentContext.setValue(scopeKey, scope), scope };
}

export function applyUpdate(target: Marks, update: Marks): void {
  Object.assign(target.pairs, update.pairs);
  if (update.unresolved) target.unresolved = true;
}

// The marks a span starts with: its local parent's saved pairs, then the
// request scope in the supplied parent context, which wins for a shared key.
export function inheritedMarks(parentContext: Context, parent: Marks | undefined): Marks {
  const scope = requestScope(parentContext);
  return {
    pairs: { ...parent?.pairs, ...scope?.pairs },
    unresolved: (parent?.unresolved ?? false) || (scope?.unresolved ?? false),
  };
}

function acceptedPair(key: string, value: unknown, parameter: string): value is FilterValue {
  if (sessionKeys.has(key)) {
    reportOnce(
      "session_key_in_context_attributes",
      `Convergent ignored a ${parameter} session key. Use session() to scope a session id.`,
    );
    return false;
  }
  if (key.startsWith("convergent.") || reservedAttributeKeys.has(key)) {
    reportOnce(
      `reserved_context_attribute:${parameter}`,
      `Convergent ignored a ${parameter} key reserved by the SDK. Use a key of your own instead.`,
    );
    return false;
  }
  if (!isScalar(value)) {
    reportOnce(
      `invalid_context_attribute:${parameter}`,
      `Convergent ignored a ${parameter} value: it must be a string, number, or boolean.`,
    );
    return false;
  }
  return true;
}

// The usable pairs in one input. A non-object is unresolved. With
// `strict`, used for resolver results and setContextAttributes(), so is an
// object whose pairs are all ignored.
export function contextUpdate(given: unknown, parameter: string, strict: boolean): Marks {
  if (given === undefined) return none;
  const failed = (): Marks => {
    reportOnce(
      `context_attributes_not_object:${parameter}`,
      `Convergent could not use ${parameter}: it takes a plain object of pairs. The span is withheld under any attribute filter.`,
    );
    return unresolvable;
  };
  if (!isMapping(given)) return failed();
  let entries: [string, unknown][];
  try {
    entries = Object.entries(given);
  } catch {
    return failed();
  }
  const pairs: Record<string, FilterValue> = {};
  for (const [key, value] of entries) {
    if (acceptedPair(key, value, parameter)) pairs[key] = value;
  }
  if (strict && entries.length > 0 && Object.keys(pairs).length === 0) return unresolvable;
  return { pairs, unresolved: false };
}

// The pairs for one call of a wrapped function, from a mapping or a resolver
// that receives the call's arguments by position.
export function resolveContextAttributes(given: unknown, args: readonly unknown[]): Marks {
  if (typeof given !== "function") return contextUpdate(given, "contextAttributes", false);
  let resolved: unknown;
  try {
    resolved = given(...args);
  } catch {
    reportOnce(
      "context_attributes_resolver_threw",
      "Convergent could not resolve contextAttributes: the resolver threw. The span is withheld under any attribute filter.",
    );
    return unresolvable;
  }
  return contextUpdate(resolved, "the contextAttributes resolver result", true);
}

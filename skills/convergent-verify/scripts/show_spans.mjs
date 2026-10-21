#!/usr/bin/env node
// Show one agent subtree from a Convergent OTLP/JSONL recording.
//
// A port of show_spans.py for machines with Node.js and no Python. It takes the
// same flags and prints the same bytes. To do that it reads JSON the way
// Python does (ints, floats, and key order kept) and formats values the way
// Python's json.dumps, repr, and str.isprintable do.

import { lstatSync, readFileSync, readdirSync, statSync } from "node:fs";

const AGENT_RUN = "invoke_agent";
const MODEL_CALL = "chat";
const TOOL_CALL = "execute_tool";
const LITELLM_SCOPE = "litellm";
const TREE_LINE_CAP = 60;
const ISSUE_SPAN_CAP = 3;

const OPERATION_ROLES = new Map([
  [AGENT_RUN, AGENT_RUN],
  [MODEL_CALL, MODEL_CALL],
  [TOOL_CALL, TOOL_CALL],
  ["text_completion", MODEL_CALL],
  ["generate_content", MODEL_CALL],
  ["invoke_workflow", "step"],
  ["retrieval", "step"],
  ["embeddings", "step"],
  ["create_agent", "step"],
]);

const LITELLM_OPERATIONS = new Map([
  ["completion", MODEL_CALL],
  ["acompletion", MODEL_CALL],
  ["completion_with_retries", MODEL_CALL],
  ["responses", MODEL_CALL],
  ["aresponses", MODEL_CALL],
  ["atext_completion", "text_completion"],
  ["embedding", "embeddings"],
  ["aembedding", "embeddings"],
]);

const SESSION_KEYS = ["gen_ai.conversation.id", "convergent.session.id", "session.id"];
const USAGE_KEYS = ["gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"];
const SUMMARY_KEYS = new Map([
  [AGENT_RUN, ["gen_ai.agent.name", "gen_ai.agent.version", ...SESSION_KEYS]],
  [MODEL_CALL, ["gen_ai.request.model", ...USAGE_KEYS]],
  [TOOL_CALL, ["gen_ai.tool.name", "gen_ai.tool.call.id"]],
]);

const MARK_PREFIX = "convergent.attributes.";

const CONTENT_KEYS = [
  ["gen_ai.input.messages", "input"],
  ["gen_ai.output.messages", "output"],
  ["gen_ai.tool.call.arguments", "arguments"],
  ["gen_ai.tool.call.result", "result"],
];
const CONTENT_LABELS = new Map(CONTENT_KEYS);
const MESSAGE_KEYS = ["gen_ai.input.messages", "gen_ai.output.messages"];

// ---------------------------------------------------------------------------
// Python value model. JSON ints are BigInt, floats are PyFloat, objects are Map
// (insertion order), arrays are Array, null is None.

class PyFloat {
  constructor(value) {
    this.value = value;
  }
}

/** An exception show_spans.py catches and prints as `error: <message>`. */
class PyError extends Error {}
class RecordingError extends PyError {}

function typeName(value) {
  if (value === null || value === undefined) return "NoneType";
  if (typeof value === "string") return "str";
  if (typeof value === "boolean") return "bool";
  if (typeof value === "bigint") return "int";
  if (value instanceof PyFloat) return "float";
  if (Array.isArray(value)) return "list";
  if (value instanceof Map) return "dict";
  return "object";
}

const isDict = (value) => value instanceof Map;
const isStr = (value) => typeof value === "string";

function truthy(value) {
  if (value === null || value === undefined || value === false) return false;
  if (typeof value === "string") return value.length > 0;
  if (typeof value === "bigint") return value !== 0n;
  if (value instanceof PyFloat) return value.value !== 0;
  if (Array.isArray(value)) return value.length > 0;
  if (value instanceof Map) return value.size > 0;
  return true;
}

function pyGet(object, key, fallback = null) {
  if (!isDict(object)) {
    throw new PyError(`'${typeName(object)}' object has no attribute 'get'`);
  }
  return object.has(key) ? object.get(key) : fallback;
}

function pyIter(value) {
  if (Array.isArray(value)) return value;
  if (isDict(value)) return [...value.keys()];
  if (isStr(value)) return Array.from(value);
  throw new PyError(`'${typeName(value)}' object is not iterable`);
}

// Python's str.isspace set, used by str.split(), str.strip(), and int().
const SPACE = /[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u;
const isSpace = (character) => SPACE.test(character);
const NOT_PRINTABLE = /^[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]$/u;
const isPrintable = (character) => character === " " || !NOT_PRINTABLE.test(character);

function strip(text, { left = true, right = true } = {}) {
  const characters = Array.from(text);
  let start = 0;
  let end = characters.length;
  while (left && start < end && isSpace(characters[start])) start += 1;
  while (right && end > start && isSpace(characters[end - 1])) end -= 1;
  return characters.slice(start, end).join("");
}

function splitWhitespace(text) {
  const words = [];
  let word = "";
  for (const character of text) {
    if (isSpace(character)) {
      if (word) words.push(word);
      word = "";
    } else {
      word += character;
    }
  }
  if (word) words.push(word);
  return words;
}

/** Code point order, which is how Python compares str. */
function compareStr(a, b) {
  const left = Array.from(a);
  const right = Array.from(b);
  for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
    const difference = left[index].codePointAt(0) - right[index].codePointAt(0);
    if (difference) return difference;
  }
  return left.length - right.length;
}

const compareInt = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

function hex(code, width) {
  return code.toString(16).padStart(width, "0");
}

function reprStr(text) {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let body = "";
  for (const character of text) {
    const code = character.codePointAt(0);
    if (character === quote || character === "\\") body += `\\${character}`;
    else if (character === "\t") body += "\\t";
    else if (character === "\n") body += "\\n";
    else if (character === "\r") body += "\\r";
    else if (code < 0x20 || code === 0x7f) body += `\\x${hex(code, 2)}`;
    else if (code < 0x7f || isPrintable(character)) body += character;
    else if (code <= 0xff) body += `\\x${hex(code, 2)}`;
    else if (code <= 0xffff) body += `\\u${hex(code, 4)}`;
    else body += `\\U${hex(code, 8)}`;
  }
  return `${quote}${body}${quote}`;
}

function reprFloat(value) {
  if (Number.isNaN(value)) return "nan";
  if (value === Infinity) return "inf";
  if (value === -Infinity) return "-inf";
  if (value === 0) return Object.is(value, -0) ? "-0.0" : "0.0";
  const sign = value < 0 ? "-" : "";
  const [mantissa, exponentText] = Math.abs(value).toExponential().split("e");
  const digits = mantissa.replace(".", "");
  const exponent = Number(exponentText);
  const point = exponent + 1;
  if (exponent < -4 || exponent >= 16) {
    const fraction = digits.length > 1 ? `.${digits.slice(1)}` : "";
    const magnitude = String(Math.abs(exponent)).padStart(2, "0");
    return `${sign}${digits[0]}${fraction}e${exponent < 0 ? "-" : "+"}${magnitude}`;
  }
  if (point <= 0) return `${sign}0.${"0".repeat(-point)}${digits}`;
  if (point >= digits.length) return `${sign}${digits}${"0".repeat(point - digits.length)}.0`;
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
}

/** Python repr() for the values JSON can produce. */
function repr(value) {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "bigint") return value.toString();
  if (value instanceof PyFloat) return reprFloat(value.value);
  if (isStr(value)) return reprStr(value);
  if (Array.isArray(value)) return `[${value.map(repr).join(", ")}]`;
  if (isDict(value)) {
    return `{${[...value].map(([key, item]) => `${repr(key)}: ${repr(item)}`).join(", ")}}`;
  }
  return String(value);
}

/** Python str(). */
const pyStr = (value) => (isStr(value) ? value : repr(value));

/** Python int() for str, int, bool, and float. */
function pyInt(value) {
  if (typeof value === "bigint") return value;
  if (typeof value === "boolean") return value ? 1n : 0n;
  if (value instanceof PyFloat) {
    if (Number.isNaN(value.value)) throw new PyError("cannot convert float NaN to integer");
    if (!Number.isFinite(value.value)) {
      // OverflowError is not caught by show_spans.py: it ends with a traceback.
      process.stderr.write("OverflowError: cannot convert float infinity to integer\n");
      process.exit(1);
    }
    return BigInt(Math.trunc(value.value));
  }
  if (isStr(value)) {
    const text = strip(value);
    if (/^[+-]?[0-9]+(_[0-9]+)*$/.test(text)) return BigInt(text.replaceAll("_", ""));
    throw new PyError(`invalid literal for int() with base 10: ${repr(value)}`);
  }
  throw new PyError(
    `int() argument must be a string, a bytes-like object or a real number, not '${typeName(value)}'`,
  );
}

/** json.dumps(value) with Python's default separators and ensure_ascii. */
function dumps(value) {
  if (value === null || value === undefined) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "bigint") return value.toString();
  if (value instanceof PyFloat) {
    if (Number.isNaN(value.value)) return "NaN";
    if (value.value === Infinity) return "Infinity";
    if (value.value === -Infinity) return "-Infinity";
    return reprFloat(value.value);
  }
  if (isStr(value)) return dumpsStr(value);
  if (Array.isArray(value)) return `[${value.map(dumps).join(", ")}]`;
  if (isDict(value)) {
    return `{${[...value].map(([key, item]) => `${dumpsStr(key)}: ${dumps(item)}`).join(", ")}}`;
  }
  return dumpsStr(repr(value));
}

const JSON_ESCAPES = { '"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f" };

function dumpsStr(text) {
  let body = "";
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    const code = text.charCodeAt(index);
    if (JSON_ESCAPES[character]) body += JSON_ESCAPES[character];
    else if (code < 0x20 || code > 0x7e) body += `\\u${hex(code, 4)}`;
    else body += character;
  }
  return `"${body}"`;
}

// ---------------------------------------------------------------------------
// json.loads, with the C decoder's error messages.

class JSONDecodeError extends Error {}

function loads(text) {
  if (text.codePointAt(0) === 0xfeff) {
    throw new JSONDecodeError("Unexpected UTF-8 BOM (decode using utf-8-sig)");
  }
  let index = 0;
  const fail = (message) => {
    throw new JSONDecodeError(message);
  };
  const skip = () => {
    while (index < text.length && " \t\n\r".includes(text[index])) index += 1;
  };

  const parseString = () => {
    let out = "";
    for (;;) {
      if (index >= text.length) fail("Unterminated string starting at");
      const character = text[index];
      index += 1;
      if (character === '"') return out;
      if (character === "\\") {
        if (index >= text.length) fail("Unterminated string starting at");
        const escape = text[index];
        index += 1;
        const simple = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
        if (escape in simple) {
          out += simple[escape];
          continue;
        }
        if (escape !== "u") fail("Invalid \\escape");
        const readUnit = () => {
          const digits = text.slice(index, index + 4);
          if (!/^[0-9a-fA-F]{4}$/.test(digits)) fail("Invalid \\uXXXX escape");
          index += 4;
          return parseInt(digits, 16);
        };
        let unit = readUnit();
        if (unit >= 0xd800 && unit <= 0xdbff && text.slice(index, index + 2) === "\\u") {
          const saved = index;
          index += 2;
          const low = readUnit();
          if (low >= 0xdc00 && low <= 0xdfff) {
            out += String.fromCharCode(unit, low);
            continue;
          }
          index = saved;
        }
        out += String.fromCharCode(unit);
        continue;
      }
      if (character < " ") fail("Invalid control character at");
      out += character;
    }
  };

  const parseValue = () => {
    const character = text[index];
    if (character === '"') {
      index += 1;
      return parseString();
    }
    if (character === "{") {
      index += 1;
      const object = new Map();
      skip();
      if (text[index] === "}") {
        index += 1;
        return object;
      }
      for (;;) {
        if (text[index] !== '"') fail("Expecting property name enclosed in double quotes");
        index += 1;
        const key = parseString();
        skip();
        if (text[index] !== ":") fail("Expecting ':' delimiter");
        index += 1;
        skip();
        if (index >= text.length) fail("Expecting value");
        object.set(key, parseValue());
        skip();
        if (text[index] === "}") {
          index += 1;
          return object;
        }
        if (text[index] !== ",") fail("Expecting ',' delimiter");
        index += 1;
        skip();
        if (text[index] === "}") fail("Illegal trailing comma before end of object");
      }
    }
    if (character === "[") {
      index += 1;
      const array = [];
      skip();
      if (text[index] === "]") {
        index += 1;
        return array;
      }
      for (;;) {
        array.push(parseValue());
        skip();
        if (text[index] === "]") {
          index += 1;
          return array;
        }
        if (text[index] !== ",") fail("Expecting ',' delimiter");
        index += 1;
        skip();
        if (text[index] === "]") fail("Illegal trailing comma before end of array");
      }
    }
    for (const [word, value] of [
      ["null", null],
      ["true", true],
      ["false", false],
      ["NaN", new PyFloat(NaN)],
      ["Infinity", new PyFloat(Infinity)],
      ["-Infinity", new PyFloat(-Infinity)],
    ]) {
      if (text.startsWith(word, index)) {
        index += word.length;
        return value;
      }
    }
    const number = /-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]+)?/y;
    number.lastIndex = index;
    const match = number.exec(text);
    if (match) {
      index = number.lastIndex;
      return match[1] || match[2] ? new PyFloat(Number(match[0])) : BigInt(match[0]);
    }
    fail("Expecting value");
  };

  skip();
  const value = parseValue();
  skip();
  if (index !== text.length) fail("Extra data");
  return value;
}

// ---------------------------------------------------------------------------
// The recording.

function roleOf(operation, scope) {
  const role = OPERATION_ROLES.get(operation) ?? "";
  if (role) return role;
  if (scope === LITELLM_SCOPE || scope.startsWith(`${LITELLM_SCOPE}.`)) {
    return OPERATION_ROLES.get(LITELLM_OPERATIONS.get(operation) ?? "") ?? "";
  }
  return "";
}

function makeSpan(fields) {
  const span = { ...fields };
  span.role = roleOf(span.operation, span.scope);
  const name = span.attributes.get("gen_ai.agent.name");
  span.agentName = isStr(name) && name ? name : null;
  span.durationMs =
    !span.startNs || !span.endNs || span.endNs < span.startNs
      ? null
      : Number(span.endNs - span.startNs) / 1_000_000;
  return span;
}

function value(raw) {
  if (!isDict(raw)) return raw;
  for (const key of ["stringValue", "boolValue", "doubleValue"]) {
    if (raw.has(key)) return raw.get(key);
  }
  if (raw.has("intValue")) {
    const item = raw.get("intValue");
    return isStr(item) || typeof item === "bigint" || typeof item === "boolean" ? pyInt(item) : item;
  }
  if (raw.has("arrayValue")) {
    const item = raw.get("arrayValue");
    const items = isDict(item) ? pyGet(item, "values", []) : [];
    return pyIter(items).map(value);
  }
  if (raw.has("kvlistValue")) {
    const item = raw.get("kvlistValue");
    return attributes(isDict(item) ? pyGet(item, "values", []) : []);
  }
  return pyGet(raw, "bytesValue");
}

function attributes(items) {
  const out = new Map();
  if (!Array.isArray(items)) return out;
  for (const item of items) {
    if (isDict(item) && isStr(item.get("key"))) {
      out.set(item.get("key"), value(pyGet(item, "value", new Map())));
    }
  }
  return out;
}

/** str(pathlib.Path(text)) on POSIX. */
function normalizePath(text) {
  if (!text) return ".";
  const lead = text.startsWith("//") && !text.startsWith("///") ? "//" : text.startsWith("/") ? "/" : "";
  const parts = text.split("/").filter((part) => part && part !== ".");
  return lead + parts.join("/") || ".";
}

const isDir = (path) => {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
};
const isFile = (path) => {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
};

/** Path.rglob("spans*.jsonl"), sorted the way Python sorts paths. */
function findRecordings(root) {
  const found = [];
  const walk = (directory) => {
    let entries;
    try {
      entries = readdirSync(directory === "." ? "." : directory);
    } catch {
      return;
    }
    for (const name of entries) {
      const path = directory === "." ? name : `${directory}/${name}`;
      if (name.startsWith("spans") && name.endsWith(".jsonl") && name.length >= 11) found.push(path);
      let stat;
      try {
        stat = lstatSync(path);
      } catch {
        continue;
      }
      if (stat.isDirectory()) walk(path);
    }
  };
  walk(root);
  return found.sort((a, b) => {
    const left = a.split("/");
    const right = b.split("/");
    for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
      const order = compareStr(left[index], right[index]);
      if (order) return order;
    }
    return left.length - right.length;
  });
}

function recordingPaths(target) {
  if (isDir(target)) {
    const paths = findRecordings(target);
    if (!paths.length) throw new RecordingError(`no spans*.jsonl file exists under ${target}`);
    return paths;
  }
  if (!isFile(target)) throw new RecordingError(`no such file: ${target}`);
  return [target];
}

const ERRNO = { EACCES: [13, "Permission denied"], EISDIR: [21, "Is a directory"], ENOENT: [2, "No such file or directory"] };

function readText(path) {
  try {
    return new TextDecoder("utf-8", { ignoreBOM: true }).decode(readFileSync(path));
  } catch (error) {
    const known = ERRNO[error.code];
    if (known) throw new PyError(`[Errno ${known[0]}] ${known[1]}: ${reprStr(path)}`);
    throw new PyError(error.message);
  }
}

const or = (item, fallback) => (truthy(item) ? item : fallback);

function readRecording(target) {
  const paths = recordingPaths(target);
  const spans = [];
  for (const path of paths) {
    // Python's text mode splits lines on \n, \r, and \r\n.
    const lines = readText(path).split(/(?<=\r\n|\n|\r(?!\n))/);
    lines.forEach((line, offset) => {
      const number = offset + 1;
      if (!strip(line)) return;
      let payload;
      try {
        payload = loads(line);
      } catch (error) {
        if (!(error instanceof JSONDecodeError)) throw error;
        throw new RecordingError(`${path} line ${number} is not valid JSON: ${error.message}`);
      }
      if (!isDict(payload)) throw new RecordingError(`${path} line ${number} is not an OTLP object`);
      for (const resourceSpan of pyIter(or(pyGet(payload, "resourceSpans", []), []))) {
        const resourceOf = pyGet(resourceSpan, "resource");
        const resource = attributes(pyGet(truthy(resourceOf) ? resourceOf : new Map(), "attributes"));
        const scopeSpans = pyGet(resourceSpan, "scopeSpans", []);
        for (const scopeSpan of pyIter(truthy(scopeSpans) ? scopeSpans : [])) {
          const scopeOf = pyGet(scopeSpan, "scope");
          const scopeName = pyGet(truthy(scopeOf) ? scopeOf : new Map(), "name");
          const scope = truthy(scopeName) ? scopeName : "unknown";
          const rawSpans = pyGet(scopeSpan, "spans", []);
          for (const raw of pyIter(truthy(rawSpans) ? rawSpans : [])) {
            const spanAttributes = attributes(pyGet(raw, "attributes"));
            const operation = spanAttributes.get("gen_ai.operation.name");
            const statusOf = pyGet(raw, "status");
            const code = pyGet(truthy(statusOf) ? statusOf : new Map(), "code");
            spans.push(
              makeSpan({
                traceId: pyStr(or(pyGet(raw, "traceId"), "")),
                spanId: pyStr(or(pyGet(raw, "spanId"), "")),
                parentId: pyStr(or(pyGet(raw, "parentSpanId"), "")),
                name: pyStr(or(pyGet(raw, "name"), "(unnamed)")),
                operation: isStr(operation) ? operation : "",
                scope: pyStr(scope),
                attributes: spanAttributes,
                resource,
                startNs: pyInt(or(pyGet(raw, "startTimeUnixNano"), 0n)),
                endNs: pyInt(or(pyGet(raw, "endTimeUnixNano"), 0n)),
                status: pyStr(or(code, "STATUS_CODE_UNSET")),
              }),
            );
          }
        }
      }
    });
  }
  if (!spans.length) throw new RecordingError("the recording contains no spans");
  return [paths, spans];
}

const byTraceThenStart = (a, b) => compareStr(a.traceId, b.traceId) || compareInt(a.startNs, b.startNs);
const keyOf = (traceId, spanId) => JSON.stringify([traceId, spanId]);
const sortedStrings = (values) => [...new Set(values)].sort(compareStr);

function select(spans, agent) {
  const agentSpans = spans.filter((span) => span.role === AGENT_RUN && span.agentName);
  const names = sortedStrings(agentSpans.map((span) => span.agentName));
  const candidates = () => (names.length ? names.map((name) => clip(name)).join(", ") : "none");
  if (agent === null) {
    if (names.length !== 1) throw new RecordingError(`pass --agent with one of: ${candidates()}`);
    agent = names[0];
  }
  const roots = agentSpans.filter((span) => span.agentName === agent);
  if (!roots.length) {
    throw new RecordingError(`agent ${repr(agent)} was not recorded; candidates: ${candidates()}`);
  }

  const children = new Map();
  for (const span of spans) {
    const key = keyOf(span.traceId, span.parentId);
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(span);
  }

  const selected = new Map();
  const pending = [...roots].reverse();
  while (pending.length) {
    const span = pending.pop();
    const key = keyOf(span.traceId, span.spanId);
    if (selected.has(key)) continue;
    selected.set(key, span);
    pending.push(...[...(children.get(key) ?? [])].reverse());
  }
  return [roots, [...selected.values()].sort(byTraceThenStart)];
}

function clip(item, limit = 160) {
  let text = isStr(item) ? item : dumps(item);
  text = splitWhitespace(text).join(" ");
  const characters = Array.from(text).map((character) =>
    isPrintable(character) ? character : reprStr(character).slice(1, -1),
  );
  const clipped = Array.from(characters.join(""));
  return clipped.length <= limit
    ? clipped.join("")
    : `${clipped.slice(0, limit).join("")}... [${clipped.length} chars]`;
}

/** f"{value:.0f}": round half to even on the exact double. */
function formatWhole(number) {
  let whole = Math.floor(number);
  const rest = number - whole;
  if (rest > 0.5 || (rest === 0.5 && whole % 2 !== 0)) whole += 1;
  return BigInt(whole).toString();
}

function summary(span) {
  const parts = [];
  for (const key of SUMMARY_KEYS.get(span.role) ?? []) {
    const item = span.attributes.has(key) ? span.attributes.get(key) : null;
    let label;
    if (SESSION_KEYS.includes(key)) {
      if (item === null) continue;
      label = key;
    } else {
      label = key.startsWith("gen_ai.") ? key.slice("gen_ai.".length) : key;
    }
    parts.push(item !== null ? `${label}=${clip(item)}` : `${label}=MISSING`);
  }
  for (const key of [...span.attributes.keys()].sort(compareStr)) {
    if (key.startsWith(MARK_PREFIX)) {
      parts.push(`${key.slice("convergent.".length)}=${clip(span.attributes.get(key))}`);
    }
  }
  parts.push(span.durationMs !== null ? `duration=${formatWhole(span.durationMs)}ms` : "duration=MISSING");
  if (!["STATUS_CODE_UNSET", "STATUS_CODE_OK", "0", "1"].includes(span.status)) {
    parts.push(`status=${clip(span.status)}`);
  }
  return parts.join("  ");
}

function tally(values) {
  const counter = new Map();
  for (const item of values) counter.set(item, (counter.get(item) ?? 0) + 1);
  return counter;
}

const sortedEntries = (counter) => [...counter].sort((a, b) => compareStr(a[0], b[0]));

function counts(spans, total) {
  const roles = tally(spans.map((span) => span.role));
  const operations = tally(spans.map((span) => span.operation || "(unset)"));
  const scopes = tally(spans.map((span) => span.scope));
  const models = spans.filter((span) => span.role === MODEL_CALL);
  const withUsage = models.filter((span) => USAGE_KEYS.some((key) => span.attributes.has(key))).length;
  const releaseFields = [];
  for (const span of spans) {
    for (const [fields, key] of [
      [span.resource, "service.version"],
      [span.attributes, "convergent.release"],
    ]) {
      const item = fields.get(key);
      if (isStr(item) && item) releaseFields.push(`${key}=${clip(item)}`);
    }
  }
  const marks = tally(
    spans.flatMap((span) =>
      [...span.attributes]
        .filter(([key]) => key.startsWith(MARK_PREFIX))
        .map(([key, item]) => `${key.slice(MARK_PREFIX.length)}=${clip(item)}`),
    ),
  );
  const release = sortedStrings(releaseFields);
  return [
    `selected spans: ${spans.length} of ${total} in the file`,
    `traces: ${new Set(spans.map((span) => span.traceId)).size}`,
    `agent runs: ${roles.get(AGENT_RUN) ?? 0}`,
    `model calls: ${roles.get(MODEL_CALL) ?? 0}`,
    `model calls with token usage: ${withUsage}`,
    `tool calls: ${roles.get(TOOL_CALL) ?? 0}`,
    `release fields: ${release.length ? release.join(", ") : "none"}`,
    `context attributes: ${
      marks.size
        ? sortedEntries(marks)
            .map(([mark, count]) => `${mark} (${count} spans)`)
            .join(", ")
        : "none"
    }`,
    `operations: ${sortedEntries(operations)
      .map(([key, count]) => `${clip(key)}=${count}`)
      .join(", ")}`,
    `scopes: ${sortedEntries(scopes)
      .map(([key, count]) => `${clip(key)}=${count}`)
      .join(", ")}`,
  ];
}

/**
 * Gaps the verify skill reports as issues unless the setup explains them.
 * Empty content counts as missing. A usage key counts as present even at 0, as in counts().
 */
function issues(spans) {
  const [, children] = treeIndex(spans);
  const sessionsUsed = spans.some((span) => span.role === AGENT_RUN && hasSession(span));
  const found = [];
  for (const span of spans) {
    const label = `${clip(span.name)} (span ${clip(span.spanId)})`;
    if (span.role === MODEL_CALL) {
      for (const key of MESSAGE_KEYS) {
        if (!truthy(span.attributes.get(key))) {
          found.push([`model call with no ${CONTENT_LABELS.get(key)} content`, label]);
        }
      }
      if (!USAGE_KEYS.some((key) => span.attributes.has(key))) {
        found.push(["model call with no token usage", label]);
      }
    } else if (span.role === AGENT_RUN) {
      if (!children.has(keyOf(span.traceId, span.spanId))) found.push(["agent run with no child span", label]);
      if (sessionsUsed && !hasSession(span)) found.push(["agent run with no session id", label]);
    }
  }
  return found;
}

// One count per issue kind, then the first ISSUE_SPAN_CAP spans unless full.
function renderIssues(found, { full }) {
  const groups = new Map();
  for (const [kind, label] of found) {
    if (!groups.has(kind)) groups.set(kind, []);
    groups.get(kind).push(label);
  }
  const lines = [];
  for (const [kind, labels] of groups) {
    lines.push(`${kind}: ${labels.length} span${labels.length !== 1 ? "s" : ""}`);
    const shown = full ? labels : labels.slice(0, ISSUE_SPAN_CAP);
    for (const label of shown) lines.push(`  ${label}`);
    if (shown.length < labels.length) lines.push(`  ... ${labels.length - shown.length} more; pass --full to list all`);
  }
  return lines;
}

function hasSession(span) {
  return SESSION_KEYS.some((key) => truthy(span.attributes.get(key)));
}

function treeIndex(spans) {
  const selected = new Set(spans.map((span) => keyOf(span.traceId, span.spanId)));
  const children = new Map();
  const roots = [];
  for (const span of spans) {
    const parent = keyOf(span.traceId, span.parentId);
    if (span.parentId && selected.has(parent)) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(span);
    } else {
      roots.push(span);
    }
  }
  for (const group of children.values()) group.sort((a, b) => compareInt(a.startNs, b.startNs));
  return [roots, children];
}

function contentLines(span, pad, showContent) {
  const content = CONTENT_KEYS.filter(([key]) => span.attributes.has(key)).map(([key, label]) => [
    label,
    span.attributes.get(key),
  ]);
  if (!content.length) return [];
  if (!showContent) return [`${pad}content fields: ${content.length} hidden`];
  return content.map(([label, item]) => `${pad}${label}: ${clip(item)}`);
}

function renderTree(spans, { showContent, full }) {
  const [roots, children] = treeIndex(spans);
  let lines = [];
  const limit = full ? null : TREE_LINE_CAP;
  const pending = [...roots]
    .sort(byTraceThenStart)
    .reverse()
    .map((root) => [root, "", true, true]);
  let truncated = false;
  while (pending.length) {
    if (limit !== null && lines.length >= limit) {
      truncated = true;
      break;
    }
    const [span, prefix, last, top] = pending.pop();
    const connector = top ? "" : last ? "`-- " : "|-- ";
    lines.push(strip(`${prefix}${connector}${clip(span.name)}    ${summary(span)}`, { left: false }));
    const pad = prefix + (top || last ? "    " : "|   ");
    lines.push(...contentLines(span, pad, showContent));
    const kids = children.get(keyOf(span.traceId, span.spanId)) ?? [];
    const nextPrefix = top ? prefix : prefix + (last ? "    " : "|   ");
    for (let index = kids.length - 1; index >= 0; index -= 1) {
      pending.push([kids[index], nextPrefix, index === kids.length - 1, false]);
    }
  }
  if (limit !== null && lines.length > limit) {
    lines = lines.slice(0, limit);
    truncated = true;
  }
  if (truncated) lines.push(`... tree capped at ${TREE_LINE_CAP} lines; pass --full to show all`);
  return lines;
}

const USAGE = "usage: show_spans.mjs [-h] [--agent AGENT] [--show-content] [--full] target";

function parseArgs(argv) {
  const options = { target: null, agent: null, showContent: false, full: false };
  const flags = ["--agent", "--show-content", "--full", "--help"];
  const usageError = (message) => {
    process.stderr.write(`${USAGE}\nshow_spans.mjs: error: ${message}\n`);
    process.exit(2);
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "-h") {
      process.stdout.write(`${USAGE}\n\nShow one agent subtree from a Convergent OTLP/JSONL recording.\n`);
      process.exit(0);
    }
    if (argument.startsWith("--") && argument !== "--") {
      const [name, inline] = argument.includes("=")
        ? [argument.slice(0, argument.indexOf("=")), argument.slice(argument.indexOf("=") + 1)]
        : [argument, null];
      const matches = flags.filter((flag) => flag === name || flag.startsWith(name));
      const flag = matches.includes(name) ? name : matches.length === 1 ? matches[0] : null;
      if (!flag) {
        usageError(
          matches.length
            ? `ambiguous option: ${name} could match ${matches.join(", ")}`
            : `unrecognized arguments: ${argument}`,
        );
      }
      if (flag === "--help") {
        process.stdout.write(`${USAGE}\n\nShow one agent subtree from a Convergent OTLP/JSONL recording.\n`);
        process.exit(0);
      }
      if (flag === "--agent") {
        if (inline !== null) options.agent = inline;
        else if (index + 1 < argv.length && !argv[index + 1].startsWith("-")) options.agent = argv[++index];
        else usageError("argument --agent: expected one argument");
      } else if (inline !== null) {
        usageError(`argument ${flag}: ignored explicit argument '${inline}'`);
      } else if (flag === "--show-content") {
        options.showContent = true;
      } else {
        options.full = true;
      }
    } else if (options.target === null) {
      options.target = argument;
    } else {
      usageError(`unrecognized arguments: ${argument}`);
    }
  }
  if (options.target === null) usageError("the following arguments are required: target");
  return options;
}

function main(argv) {
  const args = parseArgs(argv);
  const target = normalizePath(args.target);
  let paths;
  let spans;
  let roots;
  let selected;
  try {
    [paths, spans] = readRecording(target);
    [roots, selected] = select(spans, args.agent);
  } catch (error) {
    if (!(error instanceof PyError)) throw error;
    process.stdout.write(`error: ${error.message}\n`);
    return 2;
  }
  const names = sortedStrings(roots.map((root) => root.agentName).filter(Boolean));
  const out = [];
  out.push(`read: ${paths.map((path) => clip(path)).join(", ")}`);
  out.push(
    `selected agent: ${names.map((name) => clip(name)).join(", ")} (${roots.length} run${roots.length !== 1 ? "s" : ""})`,
  );
  out.push("counts:");
  for (const line of counts(selected, spans.length)) out.push(`  ${line}`);
  const found = issues(selected);
  out.push(found.length ? "issues:" : "issues: none");
  for (const line of renderIssues(found, { full: args.full })) out.push(`  ${line}`);
  out.push("tree:");
  for (const line of renderTree(selected, { showContent: args.showContent, full: args.full })) out.push(`  ${line}`);
  process.stdout.write(`${out.join("\n")}\n`);
  return 0;
}

process.exitCode = main(process.argv.slice(2));

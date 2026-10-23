---
title: Attach files to spans
description: Record the files a span read or wrote, such as an upload or a generated report.
---

A span can carry the files it worked with: a receipt a user uploaded, a
document handed to a model, or a report a tool wrote. Convergent stores a copy
of each file and lists it with the span's input or output.

## Record a file

Call `input_artifact()` (Python) / `inputArtifact()` (TypeScript) for a file
the span read, such as a document given to the agent or content handed to a
model. Call `output_artifact()` / `outputArtifact()` for a file the span
produced, such as the agent's deliverable or a file a tool wrote.

Each call takes a local path or the bytes themselves: `bytes` in Python, a
`Uint8Array` or `Buffer` in TypeScript. Call it on the handle that `span()`
gives you, or on the current span inside a function wrapped with `agent()`,
`tool()`, or `observe()`.

```python tab="Python"
import convergent


@convergent.agent(name="support-agent")
def handle_refund(request: RefundRequest) -> bytes:
    convergent.current_span().input_artifact(
        request.receipt_path, media_type="image/jpeg", origin="user"
    )
    with convergent.span(name="make_return_label", operation="tool_call") as call:
        label = render_return_label(request.order_id)
        call.output_artifact(label, filename="return-label.pdf", media_type="application/pdf")
    return label
```

```ts tab="TypeScript"
import { agent, currentSpan, span } from "convergent-sdk";

const handleRefund = agent({ name: "support-agent" }, async (request: RefundRequest) => {
  currentSpan().inputArtifact(request.receiptPath, { mediaType: "image/jpeg", origin: "user" });
  return await span({ name: "make_return_label", operation: "tool_call" }, async (call) => {
    const label = await renderReturnLabel(request.orderId);
    call.outputArtifact(label, { filename: "return-label.pdf", mediaType: "application/pdf" });
    return label;
  });
});
```

The filename defaults to the last part of the path. Pass `filename` when you
pass bytes, or the file shows as "Unnamed file".

Files are recorded only when an API key is set, and only on spans the SDK
opened. On a span another library opened, the call logs a warning and records
nothing.

The SDK uploads whatever path you pass. Check a path that a model or a user
chose before you pass it.

## Which span records a file

Record each file on the span that handled it, so the trace shows where the
file entered and where it left:

| The file | Record it as | On | `origin` |
| --- | --- | --- | --- |
| A user gave it to the agent | input | the top agent span, and each model span whose request carries it | `user` |
| A tool fetched it from storage, the web, or another service | output | that tool span | `external` |
| A tool made it | output | that tool span | `tool` |
| The agent produced it, such as a report | output | the tool span that wrote it, and the top agent span for the final version | `model` when a model wrote the content, `agent` when your code did |

Do not put the file's bytes in the span's messages as well. Replace a file block
in a message with a short text part, such as `[application/pdf, 6521 bytes]`.

## Pass the media type

Pass `media_type=` (Python) / `mediaType` (TypeScript) for every file. It
defaults to `application/octet-stream`, which says nothing about the file.
Trace View picks the file's icon from the type you pass, and falls back to the
filename's extension when the type is missing. When the file is opened, its
first bytes decide the type for formats they identify, such as PDF, PNG, and
JPEG. Otherwise the type you passed decides, then the extension.

Any media type is accepted. Trace View gives these an icon, and any other type
a plain file icon:

| Icon | Media type | Extension, when the type is left out |
| --- | --- | --- |
| Image | any `image/*` | png, jpg, jpeg, gif, webp, svg, heic, bmp, tif, tiff |
| Video | any `video/*` | mp4, mov, webm, mkv, avi |
| Audio | any `audio/*` | mp3, wav, m4a, ogg, flac |
| Document | `application/pdf`, `application/msword`, `application/rtf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, any other `text/*` | pdf, doc, docx, rtf, txt, md, odt |
| Spreadsheet | `text/csv`, `text/tab-separated-values`, `application/vnd.ms-excel`, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | csv, tsv, xls, xlsx, ods |
| Code | `application/json`, `application/xml`, `text/xml`, `application/yaml`, `text/yaml`, `application/javascript`, `text/javascript`, `text/html` | json, jsonl, xml, yaml, yml, html, js, ts, py, sql |
| Archive | `application/zip`, `application/gzip`, `application/x-tar`, `application/x-7z-compressed` | zip, gz, tgz, tar, 7z |

## Say who supplied the file

The span says which step recorded a file. `origin` says where the file came
from, which the span cannot: a receipt on the agent's span came from the user,
not the agent. Set it only when the span does not already say so, and leave it
out when you do not know. It is one of five values. Any other value is logged
once and left off. The file is still recorded.

| `origin` | Who supplied the file |
| --- | --- |
| `user` | an end user handed it in |
| `agent` | the agent's own code wrote it |
| `model` | a model generated it |
| `tool` | a tool produced or fetched it |
| `external` | any other system, such as storage, the web, or another service |

## Before the process exits

The call copies the file before it returns, so later changes to the file do
not reach Convergent. Copying runs on your thread and takes tens of
milliseconds for a 16 MiB file. The
upload runs in the background after the span ends. A span that the
[span filters](span-filters.md) withhold uploads nothing.

Pending uploads do not keep the process alive. In a short-lived process, flush
with uploads turned on before it exits. It waits up to 60 seconds for files
still uploading. Change the wait with `upload_timeout_ms=` (Python) /
`uploadTimeoutMs` (TypeScript).

```python tab="Python"
result = convergent.flush(uploads=True)
```

```ts tab="TypeScript"
const result = await flush({ uploads: true });
```

`result.ok` is false when a file is still waiting. In Python,
`result.pending_uploads` counts files still waiting, and
`result.dropped_uploads` counts files that did not upload. The TypeScript
result has no upload counts.

In TypeScript, the automatic drain when a script runs out of work does not wait
for uploads. Files still queued then are discarded.

## Limits

The calls never raise. A file is not recorded, and is counted in the span
attribute `convergent.artifacts.dropped`, when:

- it is larger than 16 MiB
- the span already lists 100 files
- spans that have not ended already hold 128 MiB of copies across the process
- it cannot be read
- its media type is not printable ASCII of at most 255 characters

The SDK logs each of these once. Python does not log the 100 file limit. It
only counts it. A source given as an `http://` or `https://`
URL is not recorded. Pass a local path or bytes.

At most 64 MiB of files wait for upload. A file that arrives when that queue is
full is logged and not sent. A filename longer than 1,024 characters is logged
once and left off. The file is still recorded.

If Convergent says file uploads are off for your organization, the SDK stops
uploading and listing files for 10 minutes, then asks again. If the server
rejects the API key, uploads stop for the rest of the process.

See [SpanHandle](../python/docs/reference/api.md#spanhandle) (Python) and the
[TypeScript API reference](../typescript/docs/reference/api.md) for every
argument.

---
title: Attach files to spans
description: Record the files a span read or wrote, so they open from the recording.
---

A span can carry the files it worked with: a receipt a user uploaded, a
document handed to a model, or a report a tool wrote. Convergent stores a copy
of each file and lists it with the span's input or output.

## Record a file

Call `input_artifact()` for a file the span read, and `output_artifact()` for a
file it produced. Each takes a local path or the bytes themselves. Call them on
the handle `span()` gives you, or on `convergent.current_span()` inside a
function decorated with `agent()`, `tool()`, or `observe()`.

```python
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

`filename` defaults to the path's name. Pass it when you pass bytes, or the
file shows as "Unnamed file".

## Pass the media type

Pass `media_type` for every file. It defaults to `application/octet-stream`,
which says nothing about the file. Trace View picks the file's icon from the
type you pass, and falls back to the filename's extension when the type is
missing. When the file is opened, its first bytes decide the type for formats
they identify, such as PDF, PNG, and JPEG; otherwise the type you passed, then
the extension.

Any media type is accepted. Trace View gives these an icon, and any other type
a plain file icon:

| Icon | `media_type` | Extension, when the type is left out |
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
once and left off; the file is still recorded.

| `origin` | Who supplied the file |
| --- | --- |
| `user` | an end user handed it in |
| `agent` | the agent's own code wrote it |
| `model` | a model generated it |
| `tool` | a tool produced or fetched it |
| `external` | any other system, such as storage, the web, or another service |

## Before the process exits

The call copies the file before it returns, so later changes to the file do not
reach Convergent. A background thread uploads the copy after the span ends. In
a short-lived process, call `flush(uploads=True)` before it exits. It waits up
to 60 seconds for files still uploading.

```python
result = convergent.flush(uploads=True)
```

`result.pending_uploads` counts files still waiting, and
`result.dropped_uploads` counts files that did not upload.

## Limits

The calls never raise. A file larger than 16 MiB is logged and not recorded.
Past 100 files on one span, the extra files are not recorded. The span
attribute `convergent.artifacts.dropped` counts both. See
[SpanHandle](reference/api.md#spanhandle) for every limit.

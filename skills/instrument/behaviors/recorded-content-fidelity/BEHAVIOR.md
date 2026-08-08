---
name: recorded-content-fidelity
description: Recorded inputs are the prompts the model was sent, and recorded outputs are the answers it gave.
---

# Recorded content fidelity

A trace is read by someone deciding what the agent did and why. These clauses
judge whether the recorded content can answer that reader.

## Inputs are the real prompts

The recorded input on a model call is the text the model was sent, including
the system prompt where the call carries one. A serialized object dump in
place of the text is a violation: a Python repr such as
`[Message(role='user', content='...')]` or `<PromptTemplate object at 0x104f3b2e0>`
records the shape of the code rather than what the model read.

## Outputs are the real answers

The recorded output on a model call is the text the model returned. A response
object's repr, a wrapper class name, or a fragment of the transport payload in
place of the answer is a violation.

## Content is present and whole

A model call span with empty input or empty output is a violation. Content
truncated to the point that a reader cannot tell what was asked or answered is
a violation. Name the spans and quote enough of the recorded value to show the
problem.

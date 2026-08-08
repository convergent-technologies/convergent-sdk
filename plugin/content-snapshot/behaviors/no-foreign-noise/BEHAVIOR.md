---
name: no-foreign-noise
description: Product trace attributes describe the agent's work, and infrastructure and billing metadata stays off them.
---

# No foreign noise

A product span describes what the agent did. Infrastructure and billing
metadata describes the systems that run and meter it, and it has its own
channels. These clauses judge whether the recording keeps the two apart.

## Infrastructure metadata stays off product spans

Span attributes carry no API key hashes, no requester IP addresses, and no
internal team or account identifiers. Each is a violation, and the finding
names the specific keys found and the spans carrying them.

## Billing metadata stays off product spans

Span attributes carry no spend counters, credit balances, quota levels, or
billing plan identifiers. Token usage under the `gen_ai.usage.` keys is the
recording of the call itself and belongs there; a running total or a cost the
app computed does not.

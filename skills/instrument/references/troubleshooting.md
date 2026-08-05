# Troubleshooting

The Troubleshooting page is organized by symptom, each one with the check that
finds its cause fastest. It opens with `check()` and with getting the SDK's own
warnings to reach you, then works down the causes of nothing arriving: nothing
configured, traced code that never ran, a process that exited before the spans
shipped, a rejected key, and the wrong workspace. Later sections cover one run
arriving as many traces, including the case where a library such as
`pydantic-evals` opens a span nothing records and every span after it roots a new
trace, then agents missing or multiplying, traces with no version, missing
prompts, a dropped attribute, a run with no model call in it, and spans that were
recorded but never delivered.

Page: `python/docs/troubleshooting.md` in this SDK's documentation tree.

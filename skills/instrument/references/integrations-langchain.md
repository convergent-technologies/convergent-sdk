# LangChain

The LangChain page covers instrumenting LangChain itself, which records every
runnable it invokes rather than the model calls alone. It gives the
install line and the `LangchainInstrumentor().instrument()` setup, then names the
three span shapes a chain arrives as: one per chain suffixed `.workflow`, one per
step named `execute_task`, and one per model call suffixed `.chat`. It also says
that a chain built with `|` arrives named `RunnableSequence` until you name it.

Page: `python/docs/integrations/langchain.md` in this SDK's documentation tree.

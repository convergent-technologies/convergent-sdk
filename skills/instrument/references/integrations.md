# Integrations

The Integrations page covers how the calls a library already makes become spans.
It gives the install, `init()`, instrument order that enables any package, and
the table of model clients with the package, the minimum version, and the
instrumentor class for each. It says which attributes land on a model span,
which packages need a capture variable set before prompts and completions are
recorded, and why two packages wrapping the same call double every token count.
A combined OpenLLMetry and OpenInference registry table lists the libraries
covered without a row of their own, and the page closes with what to do when no
package covers your library.

Page: `python/docs/integrations/index.md` in https://github.com/convergent-technologies/convergent-sdk.

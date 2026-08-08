# Attribute support

The Attribute support page maps every attribute spelling Convergent reads to the
OpenTelemetry GenAI name it normalizes to. It covers four producer conventions,
OpenTelemetry GenAI, OpenLLMetry and Traceloop, litellm, and OpenInference, in
the column order the receiver applies them, and says what happens to a standard
key that arrived already set. A separate table lists every spelling accepted for
a tool call's arguments and result, which are read where the producer wrote them
rather than renamed, and says that the arguments have to be an object or a JSON
string of one or the call shows as having invalid arguments. It has the tables of
accepted `gen_ai.operation.name` values for the OpenLLMetry and OpenInference
vocabularies and the operation each one becomes. A closing section names the
attributes left alone, such as request parameters other than the model.

Page: `python/docs/reference/attributes.md` in https://github.com/convergent-technologies/convergent-sdk.

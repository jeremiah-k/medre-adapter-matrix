# Cross-platform context in Matrix

Safe and full metadata modes MUST include bounded, versioned source and relation
context when its fields are valid. Off and minimal modes MUST omit this context.
The source records its platform, configured adapter, conversation, native
reference, sender claim and verification description independently of prefix
presentation. Relation hints retain canonical and scoped native target IDs when
available. Raw SDK blobs, credentials and relation binding authority MUST NOT be
copied into this context.

Inbound context MUST be validated for its supported version, field types and
size bounds. Invalid context MUST be ignored without discarding the Matrix
message. Unknown fields MUST NOT be preserved. Valid context MUST remain a
remote relay claim under Matrix-native metadata; it MUST NOT replace the actual
Matrix sender, source adapter, native reference, or canonical relations. Local
storage evidence remains the authority for native relation binding and mutation
permissions. A sender mention without a target ID MUST NOT become an exact reply.

# Native transient typing

Project configured-room Matrix ephemeral typing membership through the optional
core activity sink, preserving complete replacement-list semantics without a
second actor cache. Exclude the relay's own user and malformed optional state.

Send aggregate relay-account activity using finite native typing timeouts and
explicit clears, guarded by configured membership and encryption policy. Keep
activity outside canonical messages, durable delivery and replay.

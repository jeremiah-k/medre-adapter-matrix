# Inspect and restore an existing Matrix signing backup

Per-adapter auth status identifies the runtime store, expected signing filename,
and local file condition. Runtime warnings explain why ordinary login cannot
recreate existing account keys.

An explicit restore-signing command validates an existing SDK backup against
the authenticated account and server signing identity before private atomic
publication. It preserves existing files, tokens, device stores and server
keys. Unsupported hard-link filesystems fail closed; post-publication errors
identify that a complete file may already be present.

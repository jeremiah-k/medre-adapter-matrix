# Durable Matrix SDK sync

- Require Python 3.12+ and mindroom-nio[e2e] 1.1.5 for the Matrix provider.
- Admit committed SDK batches before acknowledgement, preserve provenance and
  recovery-loss evidence, and retire source/consumer tasks together.
- Adopt existing crypto state and the core Classic cursor without rewinding an
  adopted store. Keep the e2e extra as an installation alias.
- Document adoption rollback limits and the released SDK's POSIX store leases.

- Keep managed OAuth consumer ownership stable across token rotation and URL
  aliases without granting unrelated explicit credentials authority.
- Back off deferred acquisition and evidence commits independently, retaining
  accepted records and filtering encrypted ingress under plaintext policy.

# Encodable relay envelopes

Discard optional relay envelopes containing lone surrogate claims before
canonical persistence. Preserve valid Matrix message text and observed native
identity when peer metadata cannot be serialized.

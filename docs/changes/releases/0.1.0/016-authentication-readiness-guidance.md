# Distinguish authentication from runtime signing

Password login without an adapter selection reports that it saved credentials
without preparing cross-signing. Runtime recovery warnings name the adapter-scoped
login command and distinguish restoration from explicit identity replacement.
The SDK-free authentication path remains available and never resets signing keys.

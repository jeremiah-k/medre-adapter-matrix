# Explain Matrix readiness and durable sync progress

Health diagnostics and transition logs identify initial sync acknowledgement,
reconnection, stale progress, inactive crypto and transport/authentication
failures separately. The first durable acknowledgement is logged even after
startup. Health remains degraded until its actual readiness conditions hold.

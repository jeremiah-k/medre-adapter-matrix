# Publish Matrix sync health from native progress

The adapter reports acknowledged sync progress, reconnect degradation,
watchdog staleness and terminal sync failure through the optional runtime health
sink. Probes and notifications use the same authentication, crypto and sync
readiness calculation. A reconnect request remains degraded until acknowledged
progress proves recovery. Notifications retain startup history and never change
message acknowledgement or delivery authority.

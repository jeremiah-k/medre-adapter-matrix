# Deliver reactions as described emote messages

Every reaction the Matrix adapter delivers crosses a network boundary and is emitted through the bridge's own session identity, so a native `m.reaction` annotation is never attribution-correct: the annotation carries the bridge identity instead of the reacting source user, and Matrix annotation idempotency collapses same-key reactions from different source users into one event. Delivered reactions therefore always render as a described `m.emote` room message regardless of delivery strategy or target resolution. The described body carries the relay attribution prefix, the exact canonical reaction key, and the target text preview; an unresolvable target renders `an earlier message` instead of empty quotes.

The adapter capability now declares `reactions="fallback"` and the send path emits only `m.room.message` events. Observed inbound `m.reaction` annotations still decode to canonical reactions with explicit keys.

# Deliver reactions as described emote messages

Every reaction the Matrix adapter delivers crosses a network boundary and is emitted through the bridge's own session identity, so a native `m.reaction` annotation is never attribution-correct: the annotation carries the bridge identity instead of the reacting source user, and Matrix annotation idempotency collapses same-key reactions from different source users into one event. Delivered reactions therefore always render as a described `m.emote` room message regardless of delivery strategy or target resolution. The described body carries the relay attribution prefix, the exact canonical reaction key, and the target text preview; an unresolvable target renders `an earlier message` instead of empty quotes.

The adapter capability now declares `reactions="fallback"` and the send path emits only `m.room.message` events. Observed inbound `m.reaction` annotations still decode to canonical reactions with explicit keys.

The displayed target preview collapses authored whitespace to one line (matching MMRelay's 40-character reaction preview), without altering the canonical relation text. The complete attributed emote body, including its intentional leading newline, obeys configured character and UTF-8 byte budgets. HTML is generated from the bounded visible text, and truncation is recorded in rendering evidence.

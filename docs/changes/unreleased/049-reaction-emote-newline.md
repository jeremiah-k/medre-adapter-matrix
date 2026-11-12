# Restore the intentional reaction emote newline

Unbound reaction targets fall back to an `m.emote` whose plain body starts
with exactly one intentional newline: text clients render `* Relay` followed
by one blank line, matching the established relay presentation convention.
The HTML formatted body encodes that same single break exactly once through
its `<p>` boundary. The previous behavior derived a `<br/>` from the same
newline and stacked a second blank line in rich clients; the interim repair
removed the newline entirely and lost the intended separation for text
clients. Resolved-target reactions remain native `m.reaction` annotations
with no body and are unchanged.

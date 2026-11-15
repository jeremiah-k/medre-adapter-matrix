# Generic source-native facts in Matrix metadata

The safe Matrix bridge-context projection accepts bounded source-native facts
under the generic `source.native_facts` shape. Rendering may use a generic
source attribution supplied through the rendering context without creating a
second transport client. The pre-first-tag Matrix schema inventory and hashes
are deliberately updated; no legacy `meshtastic_*` content fields are added.

Preserve configured origin labels when supplied attribution leaves the label
unspecified. Explicit route labels and snapshot labels, including empty labels,
retain precedence over the configured source label.

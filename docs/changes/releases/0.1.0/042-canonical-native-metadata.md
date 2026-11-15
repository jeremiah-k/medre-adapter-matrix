# Canonical Matrix native metadata

Keep the Matrix-native schema limited to observed Matrix facts and bounded
MEDRE source/relation context. Ignore flat Meshtastic content fields when
normalizing authored text, sender metadata, and reactions. Native Matrix
relations retain their scoped targets; embedded peer hints remain untrusted.

Update the frozen native schema, its example, and its digest together. The
optional legacy output renderer is outside this native schema contract.

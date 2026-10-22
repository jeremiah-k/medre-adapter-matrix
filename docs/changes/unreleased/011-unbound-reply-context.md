# Retain unbound reply context

- Preserve readable parent context and answer text when a reply cannot bind to
  a native event in the destination Matrix room.
- Keep reply precedence independent of relation order and record the explicit
  relation fallback without inventing a native relation or changing attribution.

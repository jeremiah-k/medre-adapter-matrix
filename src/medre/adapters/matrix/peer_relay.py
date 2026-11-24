"""Peer-relay trust gate helpers for inbound Matrix room events.

The adapter trusts no other relays or bridges by default.  A message that
carries the reserved MEDRE content namespace (``content["medre"]``) is
admitted only when its wire sender is listed in ``MatrixConfig.trusted_relays``;
every other sender's envelope-carrying message is dropped before decode so a
malformed or unsupported envelope can never pass through as ordinary text.

This module holds the two policy primitives the gate needs: raw namespace
detection (no parsing of claim contents) and the bounded warning throttle
that keeps repeated drops visible without flooding the operational log.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

MEDRE_CONTENT_KEY = "medre"

DEFAULT_WARNING_INTERVAL_SECONDS = 300.0
DEFAULT_WARNING_MAX_ENTRIES = 256


def carries_medre_content(content: Any) -> bool:
    """Return whether raw event content carries the reserved MEDRE namespace.

    Trust is decided from the original Matrix event, never from codec output:
    a broken envelope must not bypass the gate by failing canonicalization.
    The check is a plain membership test on the content mapping, so claim
    contents are never parsed or logged on this path.  Ordinary Matrix
    messages without the namespace are unaffected.
    """
    return isinstance(content, dict) and MEDRE_CONTENT_KEY in content


class PeerRelayWarningLimiter:
    """Bounded, monotonic per-sender/reason warning throttle for dropped envelopes.

    At most one WARNING per (*sender*, *reason*) pair per interval is emitted;
    every drop remains observable at DEBUG.  The entry map is bounded so a
    room with many distinct senders cannot grow it without limit.
    """

    def __init__(
        self,
        interval_seconds: float = DEFAULT_WARNING_INTERVAL_SECONDS,
        max_entries: int = DEFAULT_WARNING_MAX_ENTRIES,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.max_entries = max_entries
        self._last: OrderedDict[tuple[str, str], float] = OrderedDict()

    def should_warn(self, sender: str, reason: str, now: float) -> bool:
        """Return whether a drop for (*sender*, *reason*) should warn now.

        The first observation warns and starts the interval; a repeat inside
        the interval stays silent, and the next observation at or past the
        interval warns again.
        """
        key = (sender, reason)
        last = self._last.get(key)
        warn = last is None or now < last or now - last >= self.interval_seconds
        if warn:
            self._last[key] = now
        self._last.move_to_end(key)
        if len(self._last) > self.max_entries:
            self._last.popitem(last=False)
        return warn

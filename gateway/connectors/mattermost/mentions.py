"""Room-wide mention detection for Mattermost.

Mattermost's special mention keywords (@channel, @all, @here) are not real
user accounts, so they never appear in the WS `posted` event's `mentions`
user-id array (confirmed empirically for a self-mention; the array is
populated by the server's per-user notification logic, which has no user id
to attach for these keywords). Detecting them therefore requires a text
regex on the message body, unlike a real @mention of the bot which is
checked against the trusted `mentions` array (see normalize.py).

Caveat: the regex-based part of this module is based on Mattermost's
documented special-mention keywords, not confirmed against a live server —
posting an actual @channel/@all message during development would broadcast
to an entire team, which is out of scope for a connector-development probe.

SECURITY NOTE (accepted platform limitation, flagged in code review): because
Mattermost gives no ID-based/trusted signal for @channel/@all/@here, this
text regex is the ONLY signal available — unlike RC's is_room_wide_mention
equivalent, which only ever consults server-populated `mentions[]` metadata
and never touches message text. This means any already-allow-listed sender
can bypass filter_mm_message's require_mention gate by typing the literal
string "@channel"/"@all"/"@here", whether or not Mattermost's server actually
delivered it as a real channel-wide notification (e.g. the sender may lack
channel-wide-mention permission, or the workspace disables it). This also
feeds `IncomingMessage.mentions` (normalize.py unconditionally appends "all"
when this matches) and therefore MattermostConnector._compute_to_field's
`to:` field — so spoofed text can make peer agents see `to: @all` for a
message that was never a real broadcast, which could nudge agent-chain
participants toward replying more than intended (see
gateway/contexts/mm-gateway-context.md's token-multiplication guidance).
This does not allow breaking out of the bracketed format_prompt_prefix
header itself, nor does it let a sender outside the allow-list in — it only
weakens the require_mention gate's integrity for senders already trusted
enough to talk to the bot. No better technical fix exists without Mattermost
exposing a trusted channel-wide-mention signal; documented here and in
docs/supported-features.md's "Mattermost Specific" section rather than
silently accepted.
"""

from __future__ import annotations

import functools
import re

ROOM_WIDE_MENTIONS = frozenset({"all", "channel", "here"})


def is_room_wide_mention(username: str) -> bool:
    # Note: normalize.py always normalizes a detected @channel/@all/@here
    # text match to the single string "all" before it ever reaches
    # msg.mentions (the only place this predicate is currently called, from
    # MattermostConnector._compute_to_field) — so in practice only the "all"
    # branch is exercised today. The "channel"/"here" branches are kept for
    # robustness/future callers that might pass Mattermost's other raw
    # keywords directly, and to mirror RC's is_room_wide_mention shape.
    return username in ROOM_WIDE_MENTIONS


@functools.lru_cache(maxsize=1)
def _room_wide_mention_pattern() -> re.Pattern[str]:
    keywords = "|".join(re.escape(k) for k in ROOM_WIDE_MENTIONS)
    return re.compile(rf"(?<![\w@])@(?:{keywords})(?![\w.-])")


def text_has_room_wide_mention(text: str) -> bool:
    """Detect @channel / @all / @here in raw message text."""
    return bool(_room_wide_mention_pattern().search(text))

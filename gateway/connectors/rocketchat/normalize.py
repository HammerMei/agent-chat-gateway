"""Inbound message normalization for Rocket.Chat.

Converts raw DDP message documents into normalized IncomingMessage objects.
All RC-specific field names (u.username, mentions[], attachments[].title_link,
files[], ts.$date, etc.) are handled here and nowhere else in the codebase.

Inspired by OpenClaw's channels/plugins/normalize/ pattern: inbound
normalization and outbound delivery are kept as separate, focused modules.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...core.adapter_utils import ts_to_float as _ts_to_float
from ...core.connector import Attachment, IncomingMessage, Room, User, UserRole

if TYPE_CHECKING:
    from .config import RocketChatConfig
    from .rest import RocketChatREST

logger = logging.getLogger("agent-chat-gateway.connectors.rocketchat.normalize")


@functools.lru_cache(maxsize=8)
def _mention_pattern(bot_username: str) -> re.Pattern[str]:
    """Match an explicit standalone @mention of the bot username.

    Cached per bot_username: re.compile is called once per unique username
    rather than on every message, avoiding redundant regex compilation overhead
    in high-throughput rooms.
    """
    return re.compile(rf"(?<![\w@])@{re.escape(bot_username)}(?![\w.-])")


@functools.lru_cache(maxsize=8)
def _leading_mention_pattern(bot_username: str) -> re.Pattern[str]:
    """Match a leading bot mention prefix at the start of a message.

    Cached per bot_username for the same reason as _mention_pattern.
    """
    return re.compile(rf"^\s*@{re.escape(bot_username)}(?:\s+|[:,-]\s*)?")


# ---------------------------------------------------------------------------
# Filter: decide whether an inbound DDP doc should be processed
# ---------------------------------------------------------------------------


@dataclass
class FilterResult:
    accepted: bool
    sender: str = ""
    msg_ts: str = ""
    reason: str = ""  # debug only


def filter_rc_message(
    doc: dict,
    config: "RocketChatConfig",
    room_type: str,
    last_processed_ts: str | None,
) -> FilterResult:
    """Decide whether a raw RC DDP message document should be processed.

    Applies (in order):
      1. Skip messages from the bot itself.
      2. Skip senders not in the allow-list.
      3. For non-DM rooms: require explicit @mention of the bot
         (in mentions[], message text, or attachment descriptions).
      4. Timestamp deduplication: skip messages already processed.

    Returns a FilterResult describing the outcome.
    """
    sender = doc.get("u", {}).get("username", "")

    # 1. Skip own messages
    if sender == config.username:
        return FilterResult(accepted=False, reason="own message")

    # 2. Allow-list check
    if sender not in config.allow_senders:
        return FilterResult(
            accepted=False, sender=sender, reason="sender not in allow-list"
        )

    # 3. For channels / groups: require @mention
    if room_type != "dm":
        mentions = doc.get("mentions", [])
        bot_mentioned = any(m.get("username") == config.username for m in mentions)
        if not bot_mentioned:
            msg_text = doc.get("msg", "")
            attach_descs = " ".join(
                a.get("description", "") for a in doc.get("attachments", [])
            )
            searchable = (msg_text + " " + attach_descs).strip()
            if not _mention_pattern(config.username).search(searchable):
                return FilterResult(
                    accepted=False, sender=sender, reason="bot not mentioned"
                )

    # 4. Timestamp deduplication — numeric comparison to avoid false
    #    positives/negatives from lexicographic ordering of mixed-precision
    #    or mixed-format timestamp strings.
    msg_ts = _extract_ts(doc)
    msg_ts_f = _ts_to_float(msg_ts)
    last_ts_f = _ts_to_float(last_processed_ts)
    if msg_ts_f is not None and last_ts_f is not None and msg_ts_f <= last_ts_f:
        return FilterResult(
            accepted=False,
            sender=sender,
            msg_ts=msg_ts,
            reason=f"already processed (ts={msg_ts})",
        )

    return FilterResult(accepted=True, sender=sender, msg_ts=msg_ts)


# ---------------------------------------------------------------------------
# Normalize: convert an accepted DDP doc into IncomingMessage
# ---------------------------------------------------------------------------


async def normalize_rc_message(
    doc: dict,
    room: Room,
    sender_username: str,
    msg_ts: str,
    config: "RocketChatConfig",
    rest: "RocketChatREST",
    cache_dir: Path,
) -> IncomingMessage:
    """Convert an accepted RC DDP message document into a normalized IncomingMessage.

    Caller is responsible for running filter_rc_message() first; this function
    assumes the message has already passed all filters.

    Args:
        doc             : Raw RC DDP message document.
        room            : Resolved Room (id, name, type already known).
        sender_username : Extracted from doc["u"]["username"] by filter step.
        msg_ts          : Extracted timestamp string (from filter step).
        config          : RocketChatConfig (for role resolution, attachment settings).
        rest            : RocketChatREST (for authenticated attachment downloads).
        cache_dir       : Absolute directory path for downloaded attachments.
                          Caller ensures this is unique per watcher.
    """
    role = UserRole(config.role_of(sender_username))
    sender = User(
        id=doc.get("u", {}).get("_id", sender_username),
        username=sender_username,
        display_name=doc.get("u", {}).get("name", sender_username),
    )

    text = _extract_text(doc, room.type, config.username)
    attachments, warnings = await _download_attachments(doc, config, rest, cache_dir)
    thread_id: str | None = doc.get("tmid") or None

    return IncomingMessage(
        id=doc.get("_id", msg_ts),
        timestamp=msg_ts,
        room=room,
        sender=sender,
        role=role,
        text=text,
        attachments=attachments,
        warnings=warnings,
        thread_id=thread_id,
        raw=doc,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_ts(doc: dict) -> str:
    """Extract a sortable timestamp string from a DDP message document.

    RC timestamps are Unix-epoch milliseconds (numeric or inside ``{"$date": N}``).
    This function always returns a *string* for backwards compatibility with the
    dedup watermark stored in _RoomSubscription, but the comparison in
    ``filter_rc_message()`` now uses ``_ts_to_float()`` for numeric ordering.
    """
    ts = doc.get("ts", "")
    if isinstance(ts, dict):
        ts = ts.get("$date", "")
    return str(ts)



def _extract_text(doc: dict, room_type: str, bot_username: str) -> str:
    """Extract and clean message text from a DDP document.

    For DMs: return the raw text as-is (no @mention prefix to strip).
    For channels/groups: strip the leading @botname mention.

    If the main message body is empty (e.g. file-upload-only message), fall
    back to the first non-empty attachment description.  RC puts upload captions
    in attachments[].description rather than in the msg field.
    """
    raw_text = doc.get("msg", "")

    if room_type == "dm":
        text = raw_text.strip()
        if not text:
            text = _first_attachment_desc(
                doc, strip_mention=False, bot_username=bot_username
            )
    else:
        text = _leading_mention_pattern(bot_username).sub("", raw_text, count=1).strip()
        if not text:
            text = _first_attachment_desc(
                doc, strip_mention=True, bot_username=bot_username
            )

    return text or "(empty message)"


def _first_attachment_desc(doc: dict, strip_mention: bool, bot_username: str) -> str:
    """Return the first non-empty attachment description, optionally stripping @mention."""
    for att in doc.get("attachments", []):
        desc = att.get("description", "").strip()
        if strip_mention:
            desc = _leading_mention_pattern(bot_username).sub("", desc, count=1).strip()
        if desc:
            return desc
    return ""


async def _download_attachments(
    doc: dict,
    config: "RocketChatConfig",
    rest: "RocketChatREST",
    cache_dir: Path,
) -> tuple[list[Attachment], list[str]]:
    """Download all file attachments in a DDP document to cache_dir.

    Returns:
        A tuple of (successful_attachments, warnings).  Warnings are
        human-readable strings describing files that failed to download
        (too large, timed out, error).  These are injected into the agent
        prompt so the agent can inform the user.
    """
    rc_files = doc.get("files", [])
    rc_attachments = doc.get("attachments", [])
    if not rc_files:
        return [], []

    attach_cfg = config.attachments
    await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
    max_bytes = (
        int(attach_cfg.max_file_size_mb * 1024 * 1024)
        if attach_cfg.max_file_size_mb > 0
        else 0
    )
    warnings: list[str] = []

    # Download attachments with bounded concurrency (max 4 parallel downloads)
    sem = asyncio.Semaphore(4)

    async def _download_one(idx: int, file_info: dict) -> Attachment | None:
        file_id = file_info.get("_id", "")
        original_name = file_info.get("name", f"attachment_{idx}")
        file_size = file_info.get("size", 0)
        mime_type = file_info.get("type", "")

        # Resolve download URL: prefer title_link from the matching attachments[] entry.
        # Match by file_id first (stable), then by original filename, and fall back
        # to positional index only as a last resort — RC does not guarantee strict
        # positional alignment between files[] and attachments[].
        title_link = _find_title_link(rc_attachments, file_id, original_name, idx)
        if not title_link:
            title_link = f"/file-upload/{file_id}/{original_name}"

        # Build dest path using file_id as the unique key.
        # Both file_id and original_name are sanitized to prevent path traversal
        # (e.g. a malicious server sending file._id = "../../evil" must not
        # escape the cache directory).
        safe_file_id = (
            re.sub(r"[^\w.\-]", "_", file_id) if file_id else f"attachment_{idx}"
        )
        safe_name = re.sub(r"[^\w.\-]", "_", original_name)
        dest_path = cache_dir / f"{safe_file_id}_{safe_name}"

        # Defense-in-depth: verify the resolved destination is still under
        # cache_dir after Path normalization.  The regex sanitization above
        # blocks all ASCII traversal characters, but Unicode normalization or
        # exotic filesystem path handling could theoretically produce a resolved
        # path that escapes the intended directory.  Blocking here ensures no
        # file is ever written outside cache_dir regardless of the input.
        try:
            dest_path.resolve().relative_to(cache_dir.resolve())
        except ValueError:
            logger.error(
                "Attachment path traversal blocked: '%s' resolves outside cache_dir %s",
                f"{safe_file_id}_{safe_name}",
                cache_dir,
            )
            return None

        # Size check
        if max_bytes and file_size > max_bytes:
            size_mb = file_size / 1024 / 1024
            logger.warning(
                "Skipping attachment '%s': %.1f MB exceeds limit %.1f MB",
                original_name,
                size_mb,
                attach_cfg.max_file_size_mb,
            )
            warnings.append(
                f"[⚠️ Attachment '{original_name}' skipped — "
                f"{size_mb:.1f} MB exceeds {attach_cfg.max_file_size_mb:.0f} MB limit]"
            )
            return None

        # Download with timeout and bounded concurrency.
        # Use an atomic write pattern: download to a .tmp path first, then
        # rename on success. This prevents partial/corrupt files from being
        # cached and reused on subsequent messages if the download fails.
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
        async with sem:
            result: Attachment | None = None
            try:
                try:
                    await asyncio.wait_for(
                        rest.download_file(title_link, str(tmp_path)),
                        timeout=attach_cfg.download_timeout,
                    )
                    await asyncio.to_thread(tmp_path.rename, dest_path)
                    logger.info(
                        "Downloaded attachment '%s' -> %s", original_name, dest_path
                    )
                    result = Attachment(
                        original_name=original_name,
                        local_path=str(dest_path),
                        mime_type=mime_type,
                        size_bytes=file_size,
                    )

                except asyncio.TimeoutError:
                    logger.warning(
                        "Download timed out for attachment '%s' (limit %ds)",
                        original_name,
                        attach_cfg.download_timeout,
                    )
                    warnings.append(
                        f"[⚠️ Attachment '{original_name}' failed to download (timed out) — file not available]"
                    )
                except Exception as e:
                    logger.error("Failed to download attachment '%s': %s", original_name, e)
                    warnings.append(
                        f"[⚠️ Attachment '{original_name}' failed to download — file not available]"
                    )
            finally:
                # Always clean up the .tmp file on every exit path — including
                # asyncio.CancelledError (a BaseException, not caught by
                # `except Exception` above).  A synchronous unlink is used
                # intentionally: `await asyncio.to_thread(...)` would itself
                # be cancelled if the enclosing task is being cancelled,
                # defeating the purpose of this cleanup guard.
                # After a successful rename(), tmp_path no longer exists and
                # unlink(missing_ok=True) is a safe no-op.
                tmp_path.unlink(missing_ok=True)
        return result

    downloaded = await asyncio.gather(
        *[_download_one(i, f) for i, f in enumerate(rc_files)]
    )
    return [att for att in downloaded if att is not None], warnings


def _find_title_link(
    rc_attachments: list[dict],
    file_id: str,
    original_name: str,
    fallback_idx: int,
) -> str:
    """Find the download title_link for a file from the attachments[] array.

    RC's ``files[]`` and ``attachments[]`` are not guaranteed to maintain strict
    positional alignment.  This function prefers stable-ID matching over index:

      1. Match by ``title_link`` containing the ``file_id`` (most reliable).
      2. Match by ``title`` equal to ``original_name``.
      3. Fall back to positional index (original behaviour).

    Returns the ``title_link`` string, or ``""`` if nothing matched.
    """
    # 1. Match by file_id in title_link path (e.g. "/file-upload/<file_id>/...")
    if file_id:
        for att in rc_attachments:
            tl = att.get("title_link", "")
            if tl and file_id in tl:
                return tl

    # 2. Match by filename in title field
    if original_name:
        for att in rc_attachments:
            if att.get("title") == original_name:
                tl = att.get("title_link", "")
                if tl:
                    return tl

    # 3. Positional fallback (original behaviour)
    if fallback_idx < len(rc_attachments):
        return rc_attachments[fallback_idx].get("title_link", "")

    return ""

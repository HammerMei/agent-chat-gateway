#!/usr/bin/env python3
"""Find text that describes the durable-instructions file as keyed on the room alone.

Why this exists as a script rather than as a note. One wrong explanation — "the prompt
file is named by a digest of (connector, room_id)" — was written in six places while
`impl/path-rekey` was built, and corrected five times, each time where a reviewer pointed
rather than everywhere it lived. The fifth correction claimed the sweep was exhaustive.
It was not: the check behind that claim searched for the literal phrase
`digest of (connector, room_id)`, and the sixth copy said `the (connector, room_id)
digest` — same claim, different word order. Using a hand-picked pattern to *prove absence*
is worse than using one to find instances, because it manufactures a guarantee.

So this judges what a passage says rather than how it is worded: a hit is a window that
talks about the prompt file, ties it to room-only keying, and does not mention the
watcher scope. That catches any phrasing, and it is re-runnable by whoever changes this
next.

It is a helper, not a test: `ruff` does not lint `scripts/`, and a heuristic over prose
does not belong in the suite, where a false positive would block unrelated work. Run it
by hand after touching either key.

    uv run python scripts/check_prompt_key_claims.py

Exits non-zero if anything is suspect, so it can be dropped into a pre-push hook.

**It is a helper, not a proof.** A heuristic over prose is either leaky or noisy: the
first version required "prompt" or "durable" in the window and missed a five-line error
message; widening it then flagged eight passages of which five were correct uses of
`room_path_key`. The durable fix is not a better detector — it is stating the rule **once**
in `gateway/core/paths.py` and having code comments point there instead of restating it.
This script covers only the user-facing texts that must carry the rule themselves.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

# Anything that could carry the claim.
MENTIONS = ("path_key", "system-prompt", "acg-attachments", "room_path_key",
            "watcher_prompt_key", "durable_instructions")

# Records of what the code used to do; not claims about what it does now.
HISTORICAL = ("docs/migration-0.2.md", "docs/migration-0.3.md",
              "docs/design/dynamic-watcher-design.md")

# Lines either side. Was 3, which was too narrow for a multi-line error message: the
# claim in config.py's watcher-name error spans five lines, so its subject and its
# assertion never landed in one window. A window is a guess about how far apart the two
# halves of a claim sit, and guessing small is how a detector reports clean.
WINDOW = 6


# Three claims that are false after `impl/path-rekey`, each phrased many ways. The
# detector looks for what a passage *asserts*, not for wording — but "what it asserts"
# still has to be described, and the first version described only one of the three:
# it required the window to mention "prompt" or "durable", so a passage saying
# "those key on a digest of the connector and room id" (in config.py's name error) went
# unseen. Claiming phrasing-independence while depending on two nouns is the same mistake
# one level up, so the claims are enumerated here instead.


def _room_only_key(window: str) -> bool:
    """Says the key is (connector, room_id), with no mention of watcher scope."""
    room_only = bool(re.search(r"connector,?\s*(and\s+)?room[ _]id", window, re.I)) or \
        "room_path_key" in window
    watcher_aware = any(
        marker in window
        for marker in ("watcher_prompt_key", "watcher name", "watcher names",
                       "watcher in a room", "watcher-in-a-room", "watcher-scoped")
    )
    return room_only and not watcher_aware


def _about_prompt(window: str) -> bool:
    return "system-prompt" in window or "durable" in window or "prompt file" in window


def _about_either_path(window: str) -> bool:
    return _about_prompt(window) or "acg-attachments" in window or \
        "attachment cache" in window or "path component" in window


def _claims_name_keys_paths(window: str) -> bool:
    """Says a watcher name keys these paths, without saying that it no longer does."""
    asserts_name = bool(re.search(
        r"watcher name[s]?\b[^.]{0,120}?(key|path component|file path|orphan)",
        window, re.I,
    ))
    disclaims = bool(re.search(r"no longer|not\s+path|never the display name|used to",
                               window, re.I))
    return asserts_name and not disclaims


def is_suspect(window: str) -> bool:
    """True if this passage asserts something the re-key made false.

    Either of two claims:

    * the **prompt file** keys on the room alone — it keys on
      `watcher_prompt_key(connector, room_id, watcher_name)`;
    * a **watcher name** keys either path — neither is named after it any more, though
      the name is still one input to the prompt digest.

    A passage that mentions the room-only key while talking about *either* artifact is
    suspect too, because the attachment workspace is the only one that is room-scoped and
    a passage covering both cannot be right with one key.
    """
    if _claims_name_keys_paths(window) and _about_either_path(window):
        return True
    if _about_prompt(window) and _room_only_key(window):
        return True
    # "those paths key on (connector, room id)" while discussing path components at all:
    # true of the attachment link, false of the prompt file, so a passage that does not
    # distinguish them is wrong about one of the two.
    return "path component" in window and _room_only_key(window)


def main() -> int:
    # User-facing surfaces only. Code comments are kept correct by a different means —
    # they point at gateway/core/paths.py instead of restating which key belongs where —
    # because a heuristic over prose cannot be both quiet and complete, and tuning it was
    # oscillating between missing real claims and flagging correct uses of
    # `room_path_key`. Removing the restatements is the actual fix; this only guards the
    # texts that must state the rule because their readers will not open the source.
    files = [
        *REPO.joinpath("docs").rglob("*.md"),
        REPO / "CHANGELOG.md",
        REPO / "config.example.yaml",
        REPO / "README.md",
    ]
    hits: list[str] = []
    for path in sorted(f for f in files if f.exists()):
        rel = path.relative_to(REPO).as_posix()
        if rel in HISTORICAL:
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            if not any(m in line for m in MENTIONS):
                continue
            window = " ".join(lines[max(0, i - WINDOW):i + WINDOW + 1])
            if is_suspect(window):
                hits.append(f"{rel}:{i + 1}: {line.strip()[:110]}")

    if hits:
        print("Passages describing the prompt file as room-keyed:\n")
        print("\n".join(f"  {h}" for h in hits))
        print(
            "\nThe prompt file keys on watcher_prompt_key(connector, room_id, "
            "watcher_name).\nroom_path_key is for the attachment workspace only — see "
            "gateway/core/paths.py."
        )
        return 1

    print("No passage describes the prompt file as room-keyed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

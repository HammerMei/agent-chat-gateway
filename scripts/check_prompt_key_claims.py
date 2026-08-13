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

WINDOW = 3  # lines either side — a claim and its subject are rarely further apart


def is_suspect(window: str) -> bool:
    """True if this passage says the prompt file keys on the room alone."""
    about_prompt = "system-prompt" in window or "durable" in window
    room_only = bool(re.search(r"connector,?\s*room[ _]id", window, re.I)) or \
        "room_path_key" in window
    watcher_aware = any(
        marker in window
        for marker in ("watcher_prompt_key", "watcher name", "watcher in a room",
                       "watcher-in-a-room", "watcher-scoped")
    )
    return about_prompt and room_only and not watcher_aware


def main() -> int:
    files = [
        *REPO.joinpath("gateway").rglob("*.py"),
        *REPO.joinpath("tests").rglob("*.py"),
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

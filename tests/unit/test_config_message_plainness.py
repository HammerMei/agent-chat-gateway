"""Config errors are read by people who administer a chat server, not by us.

An operator hit `Watcher rule at index 0 ('r'): connector 'mm-x' is 'mattrmost',
which has no unsolicited inbound stream — it never reports a room the gateway
did not already name, so there is nothing for a pattern to match` and said,
fairly, that they could not tell what it wanted. The real fault was a missing
letter in `mattermost`; the message described the gateway's internals instead.

This guard exists because prose regresses quietly. Nothing fails when a new
message reaches for the vocabulary we use among ourselves, and the person who
pays is the one least able to translate it.

**What is checked, and what deliberately is not.** There is no length limit
here: a long message made of ordinary words is fine, and a short one made of
internal nouns is not. Length was the wrong axis — the owner's instruction was
to target what a regular person cannot understand, which is vocabulary and
missing next-actions, not character count. So this checks two things a message
is never improved by:

* **internal mechanism words**, the ones that require having read the design
  doc to parse;
* **design-document references** — a `§` or a `docs/design/...` pointer sends
  someone who wanted their config to work into an architecture document.

Adding a word to `_INTERNAL_VOCABULARY` is how you keep a future message honest.
Removing one to make a test pass is how this file becomes decoration.

Run with:
    uv run python -m pytest tests/unit/test_config_message_plainness.py -v
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# The files whose strings an operator reads when their config is wrong.
_USER_FACING_SOURCES = (
    _REPO / "gateway" / "config.py",
    _REPO / "gateway" / "config_validate.py",
)

# Words that describe how the gateway works rather than what the reader should
# do. Each one has been in a real message at some point.
_INTERNAL_VOCABULARY = (
    "unsolicited inbound",
    "inbound stream",
    "materialize",
    "materialized",
    "watermark",
    "dedup",
    "fail-closed",
    "fail closed",
    "predicate",
    "semaphore",
    "hydrate",
    "hydrated",
    "namespace",
    "idempotent",
    "transport",
)

# `§4.5`, `docs/design/whatever.md`, `see §2.6`. A doc pointer in a config error
# is a redirect to the wrong audience's document.
_DESIGN_REFERENCE = re.compile(r"§|docs/design/")


def _message_strings(path: Path) -> list[tuple[int, str]]:
    """Every string that ends up in front of an operator, with its line number.

    Collected from the ARGUMENTS of `ValueError(...)`, `RuntimeError(...)` and
    `.append(...)` calls, not from the file's text: the modules explain
    themselves at length in docstrings and comments, and those legitimately use
    the vocabulary this test forbids in messages. A text scan would either trip
    on the explanations or force them to be deleted, which is the opposite of
    what is wanted.
    """
    source = path.read_text()
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_message = (
            isinstance(func, ast.Name) and func.id in {"ValueError", "RuntimeError"}
        ) or (isinstance(func, ast.Attribute) and func.attr == "append")
        if not is_message:
            continue
        for piece in ast.walk(node.args[0]):
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                found.append((node.lineno, piece.value))
    return found


class TestMessagesAvoidInternalVocabulary(unittest.TestCase):
    def test_no_message_uses_a_word_only_we_understand(self):
        offenders: list[str] = []
        for path in _USER_FACING_SOURCES:
            for lineno, text in _message_strings(path):
                lowered = text.lower()
                for word in _INTERNAL_VOCABULARY:
                    if word in lowered:
                        offenders.append(
                            f"{path.name}:{lineno} uses {word!r} — {text.strip()[:90]!r}"
                        )
        self.assertEqual(
            [],
            offenders,
            "these messages describe how the gateway works instead of what the "
            "reader should change. Say it in ordinary words, or if the concept "
            "is genuinely needed, describe it:\n  " + "\n  ".join(offenders),
        )


class TestMessagesDoNotPointAtDesignDocs(unittest.TestCase):
    def test_no_message_cites_a_design_section(self):
        offenders: list[str] = []
        for path in _USER_FACING_SOURCES:
            for lineno, text in _message_strings(path):
                if _DESIGN_REFERENCE.search(text):
                    offenders.append(f"{path.name}:{lineno} — {text.strip()[:90]!r}")
        self.assertEqual(
            [],
            offenders,
            "a config error is read by someone who wants their config to work, "
            "not by someone looking for the rationale. Put the fix in the "
            "message; leave the section number in the code comment:\n  "
            + "\n  ".join(offenders),
        )


class TestTheGuardItselfWorks(unittest.TestCase):
    """A guard on the guard: if `_message_strings` stopped finding anything, both
    tests above would pass by vacuity and this file would be worthless."""

    def test_it_finds_a_realistic_number_of_messages(self):
        total = sum(len(_message_strings(p)) for p in _USER_FACING_SOURCES)
        self.assertGreater(
            total,
            80,
            "the message collector found almost nothing — the loader's error "
            "sites moved, and the plainness checks are now asserting over an "
            "empty list",
        )

    def test_it_would_catch_a_planted_offender(self):
        """Proves the matching works, without waiting for someone to regress a
        real message."""
        planted = "the watcher never materialized because the inbound stream "
        self.assertTrue(
            any(w in planted for w in _INTERNAL_VOCABULARY),
            "the vocabulary list no longer matches the phrasing it was written for",
        )
        self.assertTrue(_DESIGN_REFERENCE.search("see §2.6 for the rationale"))


if __name__ == "__main__":
    unittest.main()

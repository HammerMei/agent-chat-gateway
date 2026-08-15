"""The mark that keeps a message the gateway could not take reachable later.

**This is ACG's own bookkeeping, not a platform behaviour.** Both connectors apply
backpressure: when every processor queue is full, a message is refused and reported as
still owed. Refusing it is only honest if something remembers where to look for it, because
the per-message dedup id is forgotten precisely so a later replay can bring it back — and
the ordinary watermark cannot serve, since the next *accepted* message moves it past the
refused one for good.

So this module is shared for the reason ACG shares anything: the question is ACG's, and
asking it twice is how it ends up answered once. It is deliberately **not** an attempt to
make the connectors behave alike. Everything platform-specific stays with the platform:

* **Which branches are hand-backs** is control flow, and the two connectors' differ.
* **What else can invalidate a window.** Rocket.Chat's per-room subscriptions mean a
  membership change mid-flight has to close it; Mattermost's delivery tracks membership at
  the server (§6.2), so there is nothing equivalent to guard and none is added.
* **Why an outage window is captured at all.** Rocket.Chat resubscribes rooms one at a
  time, so a room that is live again while others are still confirming moves its watermark
  past the whole gap — that is what `_snapshot_replay_boundaries` exists for. Mattermost
  has no per-channel subscribe handshake; one connection resumes every channel at once, so
  that race does not exist and Mattermost does not get that call.

What is genuinely common is only this: a mark, and the rule for who may clear it.
"""

from __future__ import annotations


def just_before(ts: str) -> str:
    """The largest timestamp strictly below `ts`, as a replay lower bound.

    Shared because both platforms hand ACG epoch milliseconds as a string — Rocket.Chat
    from `ts.$date`, Mattermost from `create_at` — so "one millisecond below" is an exact
    value on both rather than an epsilon.

    Strictly below, not equal. A replay hands the same mark to the filter as the watermark,
    and the filter rejects `msg_ts <= last_ts` as already processed, so a bound equal to the
    message fetches it and then throws it away. That is a fix that changes nothing, and it
    is what the first version of this did.

    A value that will not parse is returned unchanged: an unusable bound beats a fabricated
    one, and the caller's alternative is no bound at all.
    """
    try:
        return str(int(float(ts)) - 1)
    except (TypeError, ValueError):
        return ts


class ReplayWindow:
    """Claim/discharge bookkeeping for one room's replay mark.

    A mixin of methods only. The two fields are declared by the dataclasses that use it
    rather than here, because both have a non-default field (`room`) and inherited defaults
    would have to come first. Declaring them at the use site also keeps them visible next to
    the watermark they qualify, which is where a reader looks for them.

    Implementors must provide::

        replay_boundary: str | None = None
        boundary_claims: int = 0
    """

    replay_boundary: str | None
    boundary_claims: int

    def claim_boundary(self, *fallbacks: str | None) -> int:
        """Record that someone still needs this window read. Returns the claim count.

        The window is the *oldest* mark anyone owes a read of, so an open one is never
        narrowed: the first truthy of the current value and the fallbacks wins, in that
        order.

        **The count is what makes this a method rather than an `or` expression at each
        site.** Two claimants routinely want the same timestamp — a hand-back that lands
        while a replay is reading the very window it is claiming — and the value cannot tell
        them apart. A batch comparing the mark it snapshotted against the mark it finds
        therefore reads "unchanged" in exactly the case the comparison exists to catch,
        closes the window, and the next accepted message moves the watermark past the
        handed-back one for good. That defect survived four review rounds.

        A fully falsy claim writes and counts nothing: there is no window to owe a read of,
        and no replay will look at this room.
        """
        boundary = next((c for c in (self.replay_boundary, *fallbacks) if c), None)
        if not boundary:
            return self.boundary_claims
        self.replay_boundary = boundary
        self.boundary_claims += 1
        return self.boundary_claims

    def discharge_boundary(self, claims_at_entry: int) -> bool:
        """Close the window, unless it was claimed again since `claims_at_entry`.

        False means it is now owed to somebody else and has been left open. Every site that
        can decide a replay has read the window goes through here — including the ones a
        long way from the snapshot, since a REST round trip is ample room for a hand-back.
        """
        if self.boundary_claims != claims_at_entry:
            return False
        self.replay_boundary = None
        self.boundary_claims = 0
        return True

    def discard_boundary(self) -> None:
        """Drop the window and every claim on it, because nobody is entitled to it now.

        Distinct from `discharge_boundary`: that one reports a window as *read*, this one
        says it should never be read. Rocket.Chat's membership removal is the only caller —
        a window that spans a removal would replay the interval the account was not a member
        for — and Mattermost has no equivalent, by §6.2.
        """
        self.replay_boundary = None
        self.boundary_claims = 0

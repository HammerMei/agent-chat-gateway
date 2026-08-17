"""The idle sweep (§2.5): a room that has gone quiet is dropped, and nothing else.

What these pin, in the design's own words: paused is never reclaimed by a
timer (§4.4); a sweep advances a watcher by at most one state; the sweep reads
the **frozen** rule, never current config; a drop must not land mid-turn or
cancel an approval an operator is still reading; and the teardown is narrower
than `_stop_processor` — the connector's room state survives, because that is
what makes the wake cheap (§2.2).

Everything runs through `run_once` with an injected clock — tests cannot sleep
fifteen days, and a free-running loop is where the #110 hang lesson lived.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from gateway.core.lifecycle_sweep import LifecycleSweep
from gateway.core.permission_state import PermissionRegistry, PermissionRequest
from gateway.core.state import WatcherState, past_idle_ttl
from tests.helpers import make_lifecycle

TZ = timezone.utc
NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=TZ)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _record(name="w1", *, idle_days=15, expire_days=15, age_days=16.0,
            paused=False, dropped_at="", rule=True, session_id="sess-1"):
    return WatcherState(
        watcher_name=name,
        session_id=session_id,
        room_id=f"room-{name}",
        room_type="channel",
        paused=paused,
        dropped_at=dropped_at,
        last_activity_at=_iso(NOW - timedelta(days=age_days)),
        rule=({"session_idle_days": idle_days,
               "session_expire_days": expire_days} if rule else {}),
        rule_name="eng" if rule else "",
        connector="rc" if rule else "",
        agent="default" if rule else "",
    )


def _resident_processor(*, busy=False):
    processor = MagicMock()
    processor.has_work_in_flight = busy
    processor.stop = AsyncMock()
    return processor


def _harness(records, *, processors=None, registry=None, pending_jobs=None):
    """A real lifecycle holding real records, its collaborators doubled."""
    from tests.helpers import MockAgentBackend, make_core_config

    connector = MagicMock()
    # `is not None` is the capture rule; a bare MagicMock return would be
    # written into the record as the watermark.
    connector.get_last_processed_ts = MagicMock(return_value=None)
    connector.unsubscribe_room = AsyncMock()
    lifecycle = make_lifecycle(connector=connector,
                               agents={"default": MockAgentBackend()},
                               config=make_core_config(),
                               permission_registry=registry)
    lifecycle._attachment_workspace = MagicMock()
    for r in records:
        lifecycle._states[r.watcher_name] = r
    for name, proc in (processors or {}).items():
        lifecycle._processors[name] = proc
    sweep = LifecycleSweep(lifecycle, now=lambda: NOW,
                           pending_jobs=pending_jobs)
    return sweep, lifecycle, connector


class TestTheSweepDropsAnIdleWatcher(unittest.IsolatedAsyncioTestCase):

    async def test_past_ttl_is_dropped_and_the_room_stays_subscribed(self):
        record = _record()
        proc = _resident_processor()
        sweep, lifecycle, connector = _harness([record], processors={"w1": proc})

        dropped = await sweep.run_once()

        self.assertEqual(dropped, ["w1"])
        # The record is settled from the sweep's own clock — one pass, one instant.
        self.assertEqual(record.dropped_at, _iso(NOW))
        # The runtime is released…
        self.assertNotIn("w1", lifecycle._processors)
        lifecycle._dispatcher.remove_processor.assert_called_once_with(
            "room-w1", proc)
        proc.stop.assert_awaited_once()
        lifecycle._maps.remove_session.assert_called_once_with("sess-1")
        lifecycle._state_store.save.assert_called()
        # …and the connector's room state is NOT: the idle drop never
        # unsubscribes (§2.2) — that is the whole difference from a pause.
        connector.unsubscribe_room.assert_not_called()

    async def test_within_ttl_is_untouched(self):
        record = _record(age_days=14.0)
        proc = _resident_processor()
        sweep, lifecycle, _ = _harness([record], processors={"w1": proc})

        self.assertEqual(await sweep.run_once(), [])
        self.assertEqual(record.dropped_at, "")
        self.assertIn("w1", lifecycle._processors)

    async def test_the_frozen_rule_decides_not_a_shared_default(self):
        """Two records the same age, different frozen TTLs — the sweep reads
        what each record carries (§2.5), so one drops and one stays."""
        short = _record(name="short", idle_days=15, age_days=16.0)
        long = _record(name="long", idle_days=30, age_days=16.0)
        sweep, lifecycle, _ = _harness(
            [short, long],
            processors={"short": _resident_processor(),
                        "long": _resident_processor()},
        )

        dropped = await sweep.run_once()

        self.assertEqual(dropped, ["short"])
        self.assertEqual(long.dropped_at, "")


class TestWhatTheTimerMustNeverTouch(unittest.IsolatedAsyncioTestCase):

    async def test_paused_is_never_reclaimed_by_a_timer(self):
        """§4.4: pause is an operator's explicit instruction, and it outranks
        every inference from inactivity — at any age."""
        record = _record(paused=True, age_days=400.0)
        sweep, lifecycle, _ = _harness(
            [record], processors={"w1": _resident_processor()})

        self.assertEqual(await sweep.run_once(), [])
        self.assertEqual(record.dropped_at, "")

    async def test_an_already_idle_record_is_not_re_idled(self):
        """An idle record takes the expiry leg or nothing — never the idle leg
        again. Restamping `dropped_at` would push expiry out forever, one
        sweep interval at a time. (That an idle record past its *expiry* TTL
        is reclaimed is the expiry suite's; this record is inside it.)"""
        record = _record(dropped_at=_iso(NOW - timedelta(days=2)),
                         age_days=400.0)
        sweep, lifecycle, _ = _harness([record])

        self.assertEqual(await sweep.run_once(), [])
        self.assertEqual(record.dropped_at, _iso(NOW - timedelta(days=2)),
                         "the existing dropped_at is not restamped")

    async def test_a_static_record_is_config_yamls_not_the_timers(self):
        record = _record(rule=False, age_days=400.0)
        sweep, lifecycle, _ = _harness(
            [record], processors={"w1": _resident_processor()})

        self.assertEqual(await sweep.run_once(), [])
        self.assertEqual(record.dropped_at, "")

    async def test_a_non_resident_record_is_boots_job(self):
        """No processor and no dropped_at reads as `failed` (§2.5), and failed
        records are retried at every start — a timer must not touch them."""
        record = _record(age_days=400.0)
        sweep, lifecycle, _ = _harness([record])  # no processor registered

        self.assertEqual(await sweep.run_once(), [])
        self.assertEqual(record.dropped_at, "")


class TestTheBusyGate(unittest.IsolatedAsyncioTestCase):

    async def test_a_turn_in_flight_defers_the_drop(self):
        record = _record(age_days=400.0)
        sweep, lifecycle, _ = _harness(
            [record], processors={"w1": _resident_processor(busy=True)})

        self.assertEqual(await sweep.run_once(), [])
        self.assertEqual(record.dropped_at, "")

    async def test_a_pending_approval_defers_the_drop(self):
        """An idle drop cannot cancel an approval an operator is still
        reading (§2.5)."""
        registry = PermissionRegistry()
        registry.register(PermissionRequest(
            request_id="ab12", tool_name="bash", tool_input={},
            room_id="room-w1", session_id="sess-1",
        ))
        record = _record(age_days=400.0)
        sweep, lifecycle, _ = _harness(
            [record], processors={"w1": _resident_processor()},
            registry=registry)

        self.assertEqual(await sweep.run_once(), [])
        self.assertEqual(record.dropped_at, "")


class TestTheSweepExpiresAStaleIdleRecord(unittest.IsolatedAsyncioTestCase):
    """The expiry leg (§2.5): the destructive one, so its gates are the doc's
    word for word — measured from `dropped_at`, frozen rule, reclaims
    everything, and a pending scheduled job holds it."""

    def _idle_record(self, *, dropped_days_ago=16.0, **kw):
        return _record(
            dropped_at=_iso(NOW - timedelta(days=dropped_days_ago)),
            age_days=dropped_days_ago + 20, **kw)

    async def test_past_expire_ttl_reclaims_everything(self):
        record = self._idle_record()
        sweep, lifecycle, connector = _harness([record])

        transitioned = await sweep.run_once()

        self.assertEqual(transitioned, ["w1"])
        self.assertIsNone(lifecycle.get_watcher_state("w1"), "the record is gone")
        lifecycle._state_store.save.assert_called()
        # The connector's room state is reclaimed — the room's next message
        # takes the untracked path and creates a FRESH watcher.
        connector.unsubscribe_room.assert_awaited_once_with(
            "room-w1", watcher_id="w1")
        # Session maps and the attachment workspace went with it.
        lifecycle._maps.remove_session.assert_called_with("sess-1")
        lifecycle._attachment_workspace.reclaim.assert_called_once()

    async def test_within_expire_ttl_is_untouched(self):
        record = self._idle_record(dropped_days_ago=14.0)
        sweep, lifecycle, connector = _harness([record])

        self.assertEqual(await sweep.run_once(), [])
        self.assertIsNotNone(lifecycle.get_watcher_state("w1"))
        connector.unsubscribe_room.assert_not_awaited()

    async def test_the_expiry_origin_is_dropped_at_not_activity(self):
        """A record idle for two days whose *activity* is forty days old must
        not expire: the expiry clock starts at the drop, which is what makes
        an outage survivable (§2.5)."""
        record = _record(dropped_at=_iso(NOW - timedelta(days=2)),
                         age_days=40.0)
        sweep, lifecycle, _ = _harness([record])

        self.assertEqual(await sweep.run_once(), [])
        self.assertIsNotNone(lifecycle.get_watcher_state("w1"))

    async def test_paused_is_never_expired(self):
        record = self._idle_record(dropped_days_ago=400.0, paused=True)
        sweep, lifecycle, _ = _harness([record])

        self.assertEqual(await sweep.run_once(), [])
        self.assertIsNotNone(lifecycle.get_watcher_state("w1"))

    async def test_a_pending_job_exempts_from_expiry_but_not_idling(self):
        """§2.5: idling a job-bearing room is harmless — the job's injection
        wakes it — but expiry deletes the record the recreation reads from,
        leaving the job pointing at nothing."""
        stale_idle = self._idle_record(name="jobbed")
        active_but_old = _record(name="quiet", age_days=16.0)
        sweep, lifecycle, _ = _harness(
            [stale_idle, active_but_old],
            processors={"quiet": _resident_processor()},
            pending_jobs=lambda name: True,
        )

        transitioned = await sweep.run_once()

        self.assertIsNotNone(lifecycle.get_watcher_state("jobbed"),
                             "expiry is held by the job")
        self.assertEqual(transitioned, ["quiet"],
                         "idling is not held by the job")
        self.assertTrue(lifecycle.get_watcher_state("quiet").dropped_at)

    async def test_a_woken_record_is_not_expired(self):
        """Between the sweep's look and the lock, a wake can make the record
        resident again — and a resident record is not idle, whatever its
        stale dropped_at momentarily says."""
        record = self._idle_record()
        sweep, lifecycle, connector = _harness(
            [record], processors={"w1": _resident_processor()})

        self.assertEqual(await sweep.run_once(), [])
        self.assertIsNotNone(lifecycle.get_watcher_state("w1"))

    async def test_one_transition_per_pass_across_the_whole_life(self):
        """The cascade §2.5 forbids, walked pass by pass: a watcher active at
        shutdown and forty days stale takes idle at the first sweep, holds at
        the second (its dropped_at is that pass's own stamp), and expires only
        a full expire-TTL later."""
        record = _record(age_days=40.0)
        proc = _resident_processor()
        sweep, lifecycle, _ = _harness([record], processors={"w1": proc})

        # Pass 1: idled, not expired.
        self.assertEqual(await sweep.run_once(), ["w1"])
        self.assertEqual(record.dropped_at, _iso(NOW))
        self.assertIsNotNone(lifecycle.get_watcher_state("w1"))

        # Pass 2, same day: nothing — the expiry clock started at pass 1.
        self.assertEqual(await sweep.run_once(), [])
        self.assertIsNotNone(lifecycle.get_watcher_state("w1"))

        # A full expire-TTL later: reclaimed.
        sweep._now = lambda: NOW + timedelta(days=15)
        self.assertEqual(await sweep.run_once(), ["w1"])
        self.assertIsNone(lifecycle.get_watcher_state("w1"))


class TestPastExpireTtl(unittest.TestCase):
    def test_no_dropped_at_answers_false(self):
        from gateway.core.state import past_expire_ttl

        self.assertFalse(past_expire_ttl(_record(age_days=400.0), NOW))

    def test_reads_the_frozen_expire_days(self):
        from gateway.core.state import past_expire_ttl

        short = _record(expire_days=15,
                        dropped_at=_iso(NOW - timedelta(days=16)))
        long = _record(expire_days=30,
                       dropped_at=_iso(NOW - timedelta(days=16)))
        self.assertTrue(past_expire_ttl(short, NOW))
        self.assertFalse(past_expire_ttl(long, NOW))


class TestPastIdleTtl(unittest.TestCase):
    """The arithmetic itself — the function boot will share (one function,
    two callers)."""

    def test_no_rule_no_ttl_no_clock_all_answer_false(self):
        self.assertFalse(past_idle_ttl(_record(rule=False, age_days=400.0), NOW))
        r = _record(age_days=400.0)
        r.rule = {"session_idle_days": None}
        self.assertFalse(past_idle_ttl(r, NOW))
        r2 = _record(age_days=400.0)
        r2.last_activity_at = ""
        self.assertFalse(past_idle_ttl(r2, NOW))

    def test_an_unreadable_clock_is_never_destructive(self):
        r = _record()
        r.last_activity_at = "not-a-timestamp"
        self.assertFalse(past_idle_ttl(r, NOW))

    def test_a_naive_stamp_is_read_as_local_time(self):
        r = _record()
        naive_old = (NOW - timedelta(days=400)).replace(tzinfo=None)
        r.last_activity_at = naive_old.isoformat(timespec="seconds")
        # 400 local-time days ago is past a 15-day TTL in any timezone.
        self.assertTrue(past_idle_ttl(r, NOW.astimezone()))

    def test_the_boundary_is_inclusive(self):
        r = _record(age_days=15.0)
        self.assertTrue(past_idle_ttl(r, NOW), "exactly the TTL is due")


if __name__ == "__main__":
    unittest.main()

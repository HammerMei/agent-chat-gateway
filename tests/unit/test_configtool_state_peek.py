"""Unit tests for gateway/configtool/state_peek.py — the read-only counts
behind the Rules tab's delete-rule warning (design §5.5).

Everything here runs against temp files passed in explicitly — the module's
real default paths belong to the daemon and must never be read (let alone
created) by a test run.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.configtool.state_peek import stranded_by_rule


class TestStrandedByRule(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _state_file(self, name: str, records: list[dict]) -> Path:
        path = self.tmp / f"state.{name}.json"
        path.write_text(json.dumps({"version": 3, "watchers": records}))
        return path

    def _jobs_file(self, jobs: list[dict]) -> Path:
        path = self.tmp / "jobs.json"
        path.write_text(json.dumps({"version": 1, "jobs": jobs}))
        return path

    def test_counts_records_and_their_jobs_across_connectors(self):
        states = [
            self._state_file("rc", [
                {"watcher_name": "rc:general", "rule_name": "my-rule"},
                {"watcher_name": "rc:dev", "rule_name": "other-rule"},
            ]),
            self._state_file("mm", [
                {"watcher_name": "mm:general", "rule_name": "my-rule"},
            ]),
        ]
        jobs = self._jobs_file([
            {"id": "j1", "watcher": "rc:general"},
            {"id": "j2", "watcher": "mm:general"},
            {"id": "j3", "watcher": "rc:dev"},  # other rule's watcher
        ])
        self.assertEqual(stranded_by_rule("my-rule", states, jobs), (2, 2))

    def test_a_rule_with_no_records_has_no_jobs_either(self):
        """Jobs match through the stranded records' watcher names — with no
        matching record there is nothing for a job to be orphaned FROM,
        even if a job's watcher happens to exist for another rule."""
        states = [self._state_file("rc", [
            {"watcher_name": "rc:general", "rule_name": "other-rule"},
        ])]
        jobs = self._jobs_file([{"id": "j1", "watcher": "rc:general"}])
        self.assertEqual(stranded_by_rule("my-rule", states, jobs), (0, 0))

    def test_missing_and_corrupt_files_contribute_nothing(self):
        garbage = self.tmp / "state.broken.json"
        garbage.write_text("{not json")
        wrong_shape = self.tmp / "state.list.json"
        wrong_shape.write_text(json.dumps(["not", "a", "dict"]))
        missing_jobs = self.tmp / "never-written-jobs.json"
        states = [
            garbage,
            wrong_shape,
            self.tmp / "state.absent.json",
            self._state_file("rc", [
                {"watcher_name": "rc:general", "rule_name": "my-rule"},
            ]),
        ]
        self.assertEqual(stranded_by_rule("my-rule", states, missing_jobs), (1, 0))

    def test_non_mapping_records_and_jobs_are_skipped(self):
        states = [self._state_file("rc", [
            "not-a-dict",
            {"watcher_name": "rc:general", "rule_name": "my-rule"},
        ])]
        path = self.tmp / "jobs.json"
        path.write_text(json.dumps({"version": 1, "jobs": [
            "not-a-dict",
            {"id": "j1", "watcher": "rc:general"},
        ]}))
        self.assertEqual(stranded_by_rule("my-rule", states, path), (1, 1))

    def test_a_completed_job_is_not_counted_as_stranded(self):
        """Codex review of #129: jobs.json retains COMPLETED jobs until the
        TTL purge, but they never fire again — counting one would make the
        delete warning claim a job keeps running when nothing will. Active
        and paused jobs both still count (resuming re-arms a paused one)."""
        states = [self._state_file("rc", [
            {"watcher_name": "rc:general", "rule_name": "my-rule"},
        ])]
        jobs = self._jobs_file([
            {"id": "j1", "watcher": "rc:general", "status": "completed"},
            {"id": "j2", "watcher": "rc:general", "status": "active"},
            {"id": "j3", "watcher": "rc:general", "status": "paused"},
            {"id": "j4", "watcher": "rc:general", "status": "cancelled"},   # terminal too
        ])
        self.assertEqual(stranded_by_rule("my-rule", states, jobs), (1, 2))

    def test_a_failed_default_enumeration_counts_nothing(self):
        """Codex round 2: state_files() itself can raise (ensure_runtime_dir
        on an uncreatable/unlistable runtime dir) BEFORE any per-file
        tolerance applies — best-effort counting must swallow that too."""
        from unittest.mock import patch

        with patch("gateway.configtool.state_peek.state_files", side_effect=OSError("nope")):
            self.assertEqual(stranded_by_rule("my-rule"), (0, 0))

    def test_a_non_string_job_watcher_is_skipped_not_raised(self):
        """Codex round 10: a non-string `watcher` is UNHASHABLE, so the
        membership test raised TypeError out of this function and crashed
        the rule-delete confirmation — the opposite of the best-effort
        contract this module states."""
        states = [self._state_file("rc", [
            {"watcher_name": "rc:general", "rule_name": "my-rule"},
        ])]
        jobs = self._jobs_file([
            {"id": "j1", "watcher": ["rc:general"]},      # unhashable
            {"id": "j2", "watcher": {"k": "v"}},          # unhashable
            {"id": "j3", "watcher": "rc:general"},        # the real one
        ])
        self.assertEqual(stranded_by_rule("my-rule", states, jobs), (1, 1))

    def test_a_record_without_a_watcher_name_still_counts_as_a_record(self):
        states = [self._state_file("rc", [
            {"rule_name": "my-rule"},  # malformed but attributable
        ])]
        jobs = self._jobs_file([])
        self.assertEqual(stranded_by_rule("my-rule", states, jobs), (1, 0))


if __name__ == "__main__":
    unittest.main()


class TestJobsAreCountedByRoomAsWellAsByHandle(unittest.TestCase):
    """A job created against a room keeps that room's id; the watcher's HANDLE
    can change (rename, then expire and recreate) while the job's `watcher`
    field keeps the old spelling. Counting by handle alone then reported zero
    jobs for a rule whose rooms still had jobs firing — and the delete warning
    said so to the operator (Codex, PR #140 round 2)."""

    def _files(self, tmp: Path, *, record_name: str, job_watcher: str):
        state = tmp / "state.rc.json"
        state.write_text(json.dumps({"watchers": [{
            "watcher_name": record_name, "rule_name": "eng", "connector": "rc",
            "room_id": "R-1", "session_id": "s",
        }]}))
        jobs = tmp / "jobs.json"
        jobs.write_text(json.dumps({"version": 2, "jobs": [{
            "id": "acg-1", "watcher": job_watcher, "connector": "rc", "room_id": "R-1",
            "status": "active", "message": "m", "cron": "* * * * *",
        }]}))
        return [state], jobs

    def test_a_stale_handle_still_counts_when_the_room_matches(self):
        from gateway.configtool.state_peek import stranded_by_rule

        tmp = Path(tempfile.mkdtemp())
        paths, jobs = self._files(tmp, record_name="rc:eng-renamed", job_watcher="rc:eng")

        records, counted = stranded_by_rule("eng", state_paths=paths, jobs_file=jobs)

        self.assertEqual((records, counted), (1, 1))

    def test_a_job_for_another_room_on_the_same_connector_is_not_counted(self):
        from gateway.configtool.state_peek import stranded_by_rule

        tmp = Path(tempfile.mkdtemp())
        paths, jobs = self._files(tmp, record_name="rc:eng", job_watcher="rc:ops")
        data = json.loads(jobs.read_text())
        data["jobs"][0]["room_id"] = "R-other"
        jobs.write_text(json.dumps(data))

        _, counted = stranded_by_rule("eng", state_paths=paths, jobs_file=jobs)

        self.assertEqual(counted, 0)


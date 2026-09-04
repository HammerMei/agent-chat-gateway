"""`config reload` through the control server, on a real `GatewayService` (#144).

Seam: `ControlServer.dispatch_command` — the same boundary the CLI crosses —
on a service booted from a config file with Script connectors and mock
agents. Assertions are what an operator sees: the plan document, the exit
code, `list` afterwards, the state file, `config-show`, AUDIT lines.

Run with:
    uv run python -m pytest tests/integration/test_config_reload.py -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import pytest

from gateway.core.state import load_state
from tests.helpers import (
    boot_gateway_service,
    gateway_config_text,
    isolate_runtime_dir,
    write_gateway_config,
)

pytestmark = pytest.mark.integration

ALL = ["active", "idle", "paused", "failed"]


class _ReloadCase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp, self.runtime = isolate_runtime_dir(self)
        self.config_path = self.tmp / "config.yaml"

    def _text(self, **kw) -> str:
        kw.setdefault("working_directory", self.tmp)
        return gateway_config_text(**kw)

    async def _boot(self, text: str | None = None):
        config = write_gateway_config(self.tmp, text=text if text is not None else self._text())
        self.service = await boot_gateway_service(self, self.tmp, self.runtime, config)
        return self.service

    def _rewrite(self, text: str) -> None:
        self.config_path.write_text(text)

    async def _dispatch(self, **request) -> dict:
        return await self.service._control.dispatch_command(request)

    async def _reload(self, *, dry_run=False) -> dict:
        return await self._dispatch(cmd="config-reload", dry_run=dry_run,
                                    config_path=str(self.config_path))

    async def _rows(self) -> dict[str, dict]:
        result = await self._dispatch(cmd="list", states=ALL)
        self.assertTrue(result["ok"], result)
        return {row["watcher_name"]: row for row in result["data"]}


class TestNoChangeAndDryRun(_ReloadCase):

    async def test_an_unchanged_file_is_a_no_op_that_exits_zero(self):
        await self._boot()
        before = await self._rows()
        result = await self._reload()
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["applied"])
        self.assertEqual(result["watchers"], [])
        self.assertEqual(await self._rows(), before)

    async def test_a_dry_run_plans_but_changes_nothing(self):
        await self._boot()
        rows = await self._rows()
        self.assertEqual(rows["script:script"]["state"], "active")
        self._rewrite(self._text(rules=[{
            "name": "w1", "agent": "default", "connector": "script",
            "rooms": {"include": ["script"]}, "session_idle_days": 3}]))

        result = await self._reload(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["changes"]["rules"]["changed"], ["w1"])
        self.assertEqual([(w["action"], w["handle"]) for w in result["watchers"]],
                         [("rematerialize", "script:script")])
        record = load_state("script")[0]
        self.assertEqual(record.rule["session_idle_days"], 15, "nothing was applied")
        self.assertEqual(self.service._config_digest,
                         self.service.describe_config()["digest"])
        self.assertNotEqual(result["digest"], self.service._config_digest,
                            "the plan carries the candidate's digest, the daemon keeps its own")

    async def test_a_description_only_edit_is_a_no_op(self):
        await self._boot()
        self._rewrite(self._text().replace("watcher_rules:\n- name: w1",
                                           "watcher_rules:\n- description: the room\n  name: w1"))
        result = await self._reload()
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["applied"])


class TestRuleChanges(_ReloadCase):

    async def test_an_edited_rule_rematerializes_the_record_and_keeps_its_session(self):
        await self._boot()
        before = (await self._rows())["script:script"]
        self._rewrite(self._text(rules=[{
            "name": "w1", "agent": "default", "connector": "script",
            "rooms": {"include": ["script"]}, "session_idle_days": 3}]))

        result = await self._reload()

        self.assertTrue(result["applied"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual([w["action"] for w in result["watchers"]], ["rematerialize"])
        after = (await self._rows())["script:script"]
        self.assertEqual(after["session_id"], before["session_id"], "the session survives")
        self.assertEqual(after["state"], "active", "the processor was restarted, not dropped")
        self.assertEqual(load_state("script")[0].rule["session_idle_days"], 3)
        self.assertEqual(self.service.describe_config()["digest"], result["digest"])

    async def test_a_removed_rule_expires_the_record_and_logs_the_session_id(self):
        await self._boot()
        session = (await self._rows())["script:script"]["session_id"]
        self._rewrite(self._text(rules=[]))

        with self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            result = await self._reload()

        self.assertEqual([(w["action"], w["reason"], w["session_id"]) for w in result["watchers"]],
                         [("expire", "no-rule-matches", session)])
        self.assertNotIn("script:script", await self._rows())
        audit = [line for line in logs.output if "AUDIT: session released" in line]
        self.assertEqual(len(audit), 1, logs.output)
        self.assertIn(session, audit[0])

    async def test_an_added_rule_on_an_eager_connector_starts_its_room(self):
        await self._boot()
        self._rewrite(self._text(rules=[
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script"]}},
            {"name": "w2", "agent": "default", "connector": "script",
             "rooms": {"include": ["ops"]}},
        ]))
        result = await self._reload()
        self.assertEqual(result["changes"]["rules"]["added"], ["w2"])
        rows = await self._rows()
        self.assertEqual(rows["script:ops"]["state"], "active")
        self.assertEqual(rows["script:script"]["state"], "active", "the other room was not touched")

    async def test_reordering_rules_is_applied_as_a_reconciliation(self):
        await self._boot(self._text(rules=[
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script"]}},
            {"name": "w2", "agent": "default", "connector": "script",
             "rooms": {"include": ["script", "ops"]}},
        ]))
        self._rewrite(self._text(rules=[
            {"name": "w2", "agent": "default", "connector": "script",
             "rooms": {"include": ["script", "ops"]}},
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script"]}},
        ]))
        result = await self._reload()
        self.assertTrue(result["changes"]["rules"]["reordered"])
        self.assertEqual([(w["action"], w["from_rule"], w["to_rule"]) for w in result["watchers"]
                          if w["handle"] == "script:script"],
                         [("rematerialize", "w1", "w2")])
        self.assertEqual(load_state("script")[0].rule_name if load_state("script")[0].room_id
                         == "script" else None, "w2")


class TestAgentChanges(_ReloadCase):

    async def test_a_changed_agent_is_rebuilt_and_its_processors_restarted(self):
        await self._boot()
        old_backend = self.service._agents["default"]
        before = (await self._rows())["script:script"]
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))

        result = await self._reload()

        self.assertEqual(result["changes"]["agents"]["changed"], ["default"])
        self.assertEqual([(w["action"], w["reason"]) for w in result["watchers"]],
                         [("restart", "agent 'default' restarts")])
        self.assertIsNot(self.service._agents["default"], old_backend)
        after = (await self._rows())["script:script"]
        self.assertEqual(after["state"], "active")
        self.assertEqual(after["session_id"], before["session_id"],
                         "same backend identity — the session is reused")
        self.assertEqual(self.service._core_config.agent_config("default").timeout, 99)

    async def test_a_changed_working_directory_starts_a_fresh_session_and_logs_the_old(self):
        await self._boot()
        old_session = (await self._rows())["script:script"]["session_id"]
        other = self.tmp / "elsewhere"
        other.mkdir()
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(other)}}))

        with self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            result = await self._reload()

        self.assertEqual(result["exit_code"], 0, result)
        after = (await self._rows())["script:script"]
        self.assertNotEqual(after["session_id"], old_session)
        self.assertEqual(after["state"], "active")
        new_backend = self.service._agents["default"]
        self.assertEqual([s["working_directory"] for s in new_backend.created_sessions],
                         [str(other)], "one fresh session, in the new directory")
        audit = [line for line in logs.output if "AUDIT: session released" in line
                 and old_session in line]
        self.assertEqual(len(audit), 1, logs.output)
        self.assertIn("abandoned at provisioning", audit[0])


class TestAgentRestartOrdering(_ReloadCase):

    async def test_processors_stop_before_their_backend_and_start_after_the_new_one(self):
        await self._boot()
        old_backend = self.service._agents["default"]
        sm = self.service._session_managers["script"]
        order: list[str] = []
        real_stop_backend = old_backend.stop
        real_stop_processor = sm._lifecycle._stop_processor

        async def _stop_backend():
            order.append("backend.stop")
            await real_stop_backend()

        async def _stop_processor(name):
            order.append("processor.stop")
            await real_stop_processor(name)

        old_backend.stop = _stop_backend
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))
        with patch.object(sm._lifecycle, "_stop_processor", side_effect=_stop_processor):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(order, ["processor.stop", "backend.stop"],
                         "the processor drains against a live backend, then the backend stops")
        after = (await self._rows())["script:script"]
        self.assertEqual(after["state"], "active")
        new_backend = self.service._agents["default"]
        self.assertIsNot(new_backend, old_backend)

    async def test_a_kept_lifecycle_learns_which_agents_are_unavailable(self):
        await self._boot()
        sm = self.service._session_managers["script"]
        self.assertEqual(sm._lifecycle._blocked_agents, set())
        self._rewrite(self._text(agents={"default": {
            "type": "claude", "working_directory": str(self.tmp), "timeout": 99}}))

        async def _boom():
            raise RuntimeError("no such binary")

        from tests.helpers import MockAgentBackend

        def _broken(cfg):
            backend = MockAgentBackend(id_prefix="broken")
            backend.start = _boom
            return backend

        with patch("gateway.service._build_agent_backend", side_effect=_broken):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertEqual([(d["kind"], d["name"]) for d in result["degraded"]],
                         [("agent", "default")])
        self.assertIn("no such binary", result["degraded"][0]["error"])
        self.assertEqual(sm._lifecycle._blocked_agents, {"default"},
                         "the kept lifecycle refuses starts on the agent that did not come up")
        status = await self._dispatch(cmd="config-show", include_config=False)
        self.assertIn("no such binary", status["degraded"][0]["error"])

        # The binary is back; an unchanged reload retries and clears the gate.
        second = await self._reload()
        self.assertEqual(second["exit_code"], 0, second)
        self.assertEqual(sm._lifecycle._blocked_agents, set())
        self.assertEqual((await self._rows())["script:script"]["state"], "active")


class TestConnectorChanges(_ReloadCase):

    def _two(self, **kw) -> str:
        return self._text(connectors=("script", "second"), rules=[
            {"name": "w1", "agent": "default", "connector": "script",
             "rooms": {"include": ["script"]}},
            {"name": "w2", "agent": "default", "connector": "second",
             "rooms": {"include": ["script"]}},
        ], **kw)

    async def test_an_added_connector_is_built_connected_and_synced(self):
        await self._boot()
        self._rewrite(self._two())
        result = await self._reload()
        self.assertEqual(result["changes"]["connectors"]["added"], ["second"])
        self.assertEqual(result["exit_code"], 0, result)
        rows = await self._rows()
        self.assertEqual(rows["second:script"]["state"], "active")
        self.assertEqual([e.name for e in self.service._entries], ["script", "second"])
        self.assertEqual(set(self.service._session_managers), {"script", "second"})

    async def test_a_removed_connector_expires_its_records_and_deletes_its_state_file(self):
        await self._boot(self._two())
        session = (await self._rows())["second:script"]["session_id"]
        self.assertTrue((self.runtime / "state.second.json").exists())
        self._rewrite(self._text())

        with self.assertLogs("agent-chat-gateway", level="WARNING") as logs:
            result = await self._reload()

        self.assertEqual(result["changes"]["connectors"]["removed"], ["second"])
        self.assertEqual([(w["action"], w["reason"], w["session_id"]) for w in result["watchers"]],
                         [("expire", "connector-removed", session)])
        self.assertFalse((self.runtime / "state.second.json").exists())
        self.assertNotIn("second:script", await self._rows())
        self.assertTrue(any("AUDIT: session released" in line and session in line
                            and "connector-removed" in line for line in logs.output))

    async def test_a_changed_connector_restarts_as_a_unit_with_records_kept(self):
        await self._boot()
        old = self.service._entries[0]
        before = (await self._rows())["script:script"]
        self._rewrite(self._text().replace("- name: script\n  type: script",
                                           "- name: script\n  type: script\n  context_inject_files: []\n  timezone: UTC"))
        # `timezone` lands in `raw`, which is a connector change.
        result = await self._reload()
        self.assertEqual(result["changes"]["connectors"]["changed"], ["script"], result)
        self.assertEqual([(w["action"], w["reason"]) for w in result["watchers"]],
                         [("restart", "connector restarts")])
        self.assertTrue(any("re-validated" in n for n in result["notes"]))
        self.assertIsNot(self.service._entries[0], old)
        self.assertIsNot(self.service._session_managers["script"], old.session_manager)
        after = (await self._rows())["script:script"]
        self.assertEqual(after["session_id"], before["session_id"])
        self.assertEqual(after["state"], "active")

    async def test_a_connector_that_fails_to_connect_is_degraded_not_fatal(self):
        await self._boot()
        self._rewrite(self._two())
        from gateway.connectors import connector_factory as real_factory

        def _factory(cc):
            connector = real_factory(cc)
            if cc.name == "second":
                async def _boom():
                    raise ConnectionError("refused")
                connector.connect = _boom
            return connector

        with patch("gateway.service.connector_factory", side_effect=_factory):
            result = await self._reload()

        self.assertEqual(result["exit_code"], 2, result)
        self.assertEqual([(d["kind"], d["name"]) for d in result["degraded"]],
                         [("connector", "second")])
        self.assertIn("refused", result["degraded"][0]["error"])
        status = await self._dispatch(cmd="config-show", include_config=False)
        self.assertEqual([d["name"] for d in status["degraded"]], ["second"])
        self.assertEqual((await self._rows())["script:script"]["state"], "active",
                         "the running connector was not touched")
        self.assertEqual([e.name for e in self.service._entries], ["script", "second"],
                         "the degraded entry stays in the mapping")
        self.assertEqual(self.service.describe_config()["digest"], result["digest"],
                         "the config IS active — the operator fixes and reloads again")

    async def test_a_degraded_connector_is_retried_by_the_next_reload_even_if_unchanged(self):
        await self._boot()
        self._rewrite(self._two())
        from gateway.connectors import connector_factory as real_factory

        def _factory(cc):
            connector = real_factory(cc)
            if cc.name == "second":
                async def _boom():
                    raise ConnectionError("refused")
                connector.connect = _boom
            return connector

        with patch("gateway.service.connector_factory", side_effect=_factory):
            first = await self._reload()
        self.assertEqual(first["exit_code"], 2)

        # Nothing in the file changed; the server is simply reachable again.
        second = await self._reload()

        self.assertEqual(second["exit_code"], 0, second)
        self.assertEqual(second["changes"]["connectors"]["changed"], ["second"])
        self.assertTrue(any("retried" in n for n in second["notes"]))
        self.assertEqual((await self._rows())["second:script"]["state"], "active")
        self.assertEqual(self.service.describe_config()["degraded"], [])

    async def test_commands_against_a_degraded_connector_are_refused_with_the_cause(self):
        await self._boot()
        self._rewrite(self._two())
        from gateway.connectors import connector_factory as real_factory

        def _factory(cc):
            connector = real_factory(cc)
            if cc.name == "second":
                async def _boom():
                    raise ConnectionError("refused")
                connector.connect = _boom
            return connector

        with patch("gateway.service.connector_factory", side_effect=_factory):
            await self._reload()

        resumed = await self._dispatch(cmd="resume", connector="second",
                                       watcher_name="second:script")
        self.assertFalse(resumed["ok"])
        self.assertIn("degraded", resumed["error"])
        self.assertIn("refused", resumed["error"])
        self.assertNotIn("shutting down", resumed["error"])
        sent = await self._dispatch(cmd="send", connector="second", room="script", text="hi")
        self.assertFalse(sent["ok"])
        self.assertIn("degraded", sent["error"])
        listed = await self._dispatch(cmd="list", connector="second", states=ALL)
        self.assertTrue(listed["ok"], "list still answers for the records")

    async def test_an_apply_that_raises_leaves_a_candidate_shaped_fleet(self):
        """A defect mid-apply must not wedge the daemon or make it lie."""
        await self._boot()
        self._rewrite(self._two())
        sm = self.service._session_managers["script"]
        with patch.object(sm, "replace_rules", side_effect=RuntimeError("kaboom")):
            result = await self._reload()

        self.assertFalse(result["ok"])
        self.assertIn("kaboom", result["error"])
        self.assertEqual([e.name for e in self.service._entries], ["script", "second"],
                         "every connector the candidate names has an entry")
        self.assertTrue(self.service._entries[1].degraded)
        self.assertFalse(sm._lifecycle.transitions_disarmed, "the kept manager is re-armed")
        self.assertIsNotNone(self.service._scheduler_task)
        status = await self._dispatch(cmd="config-show", include_config=False)
        self.assertEqual([d["name"] for d in status["degraded"]], ["second"])
        # The candidate is active: the next reload diffs against it and retries.
        second = await self._reload()
        self.assertEqual(second["exit_code"], 0, second)
        self.assertEqual((await self._rows())["second:script"]["state"], "active")

    async def test_a_scheduled_job_survives_its_connectors_restart(self):
        await self._boot()
        created = await self._dispatch(cmd="schedule-create", watcher="script:script",
                                       message="ping", cron="0 9 * * *", times=0)
        self.assertTrue(created["ok"], created)
        self._rewrite(self._text().replace("- name: script\n  type: script",
                                           "- name: script\n  type: script\n  timezone: UTC"))
        result = await self._reload()
        self.assertEqual(result["changes"]["connectors"]["changed"], ["script"])
        jobs = await self._dispatch(cmd="schedule-list")
        self.assertEqual([j["status"] for j in jobs["jobs"]], ["active"], jobs)
        self.assertIsNotNone(self.service._scheduler_task, "the scheduler is running again")


class TestValuesAndRefusals(_ReloadCase):

    async def test_max_queue_depth_is_swapped_in_place_without_restarts(self):
        await self._boot()
        self._rewrite(self._text(extra="max_queue_depth: 7\n"))
        result = await self._reload()
        self.assertEqual(result["changes"]["values"],
                         [{"path": "max_queue_depth", "old": 100, "new": 7}])
        self.assertEqual(result["watchers"], [])
        self.assertEqual(self.service._core_config.max_queue_depth, 7)

    async def test_an_invalid_file_is_refused_with_findings_and_nothing_changes(self):
        await self._boot()
        digest = self.service.describe_config()["digest"]
        self._rewrite(self._text(rules=[{
            "name": "w1", "agent": "nobody", "connector": "script",
            "rooms": {"include": ["script"]}}]))
        result = await self._reload()
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertTrue(any("nobody" in f["message"] for f in result["validation"]["findings"]),
                        result)
        self.assertEqual(self.service.describe_config()["digest"], digest)
        self.assertEqual((await self._rows())["script:script"]["state"], "active")

    async def test_a_second_reload_while_one_applies_is_refused(self):
        await self._boot()
        async with self.service._reload_lock:
            result = await self._reload()
        self.assertFalse(result["ok"])
        self.assertIn("already in progress", result["error"])

    async def test_lifecycle_verbs_are_refused_during_the_apply_window(self):
        await self._boot()
        self.service._reloading = True
        try:
            result = await self._dispatch(cmd="pause", watcher_name="script:script")
        finally:
            self.service._reloading = False
        self.assertFalse(result["ok"])
        self.assertIn("reload is in progress", result["error"])
        listed = await self._dispatch(cmd="list", states=ALL)
        self.assertTrue(listed["ok"], "list is not a lifecycle verb")

    async def test_another_path_than_the_daemons_is_refused(self):
        await self._boot()
        result = await self._dispatch(cmd="config-reload", dry_run=True,
                                      config_path=str(self.tmp / "other.yaml"))
        self.assertFalse(result["ok"])
        self.assertIn("runs", result["error"])

    async def test_config_show_returns_the_redacted_active_config(self):
        await self._boot()
        result = await self._dispatch(cmd="config-show")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["digest"]), 64)
        self.assertEqual(result["config"]["connectors"][0]["name"], "script")
        self.assertEqual(result["config_path"], str(self.config_path.resolve()))
        self.assertTrue(result["loaded_at"])


class TestApplyIsQuiescent(_ReloadCase):

    async def test_kept_managers_are_rearmed_after_the_apply(self):
        await self._boot()
        self._rewrite(self._text(extra="max_queue_depth: 7\n"))
        await self._reload()
        sm = self.service._session_managers["script"]
        self.assertFalse(sm._lifecycle.transitions_disarmed)
        paused = await self._dispatch(cmd="pause", watcher_name="script:script")
        self.assertTrue(paused["ok"], paused)

    async def test_the_scheduler_is_paused_across_the_apply_and_restarted(self):
        await self._boot()
        seen: list[bool] = []
        original = self.service._install_entries

        def _spy(entries):
            seen.append(self.service._scheduler_task is None)
            original(entries)

        self._rewrite(self._text(extra="max_queue_depth: 7\n"))
        with patch.object(self.service, "_install_entries", side_effect=_spy):
            await self._reload()
        self.assertTrue(seen and all(seen), "no scheduler task ran during the apply")
        self.assertIsNotNone(self.service._scheduler_task)
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()

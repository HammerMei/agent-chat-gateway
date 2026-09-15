"""The role-enforcement plugin's hook, exercised under bun (#165).

The plugin is TypeScript with no test harness of its own; opencode bundles bun,
and developer machines that run opencode have it. These tests are skipped
where `bun` is not on PATH (CI today), so the live check in the PR is what
covers them there — the point of having them is that a change to the hook's
contract fails locally before a reviewer has to trace it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from gateway.agents.opencode.plugin import PLUGIN_SOURCE

_RUNNER = """
const mod = await import(process.argv[2]);
const hooks = mod.default();
const [tool, envJson] = [process.argv[3], process.argv[4]];
for (const [k, v] of Object.entries(JSON.parse(envJson))) process.env[k] = v;
const output = {};
try {
  await hooks["tool.execute.before"]({ tool, sessionID: "s", callID: "c" }, output);
  console.log(JSON.stringify({ threw: null, status: output.status ?? null }));
} catch (e) {
  console.log(JSON.stringify({ threw: String(e.message), status: output.status ?? null }));
}
"""


def _run_hook(tool: str, env: dict[str, str]) -> dict:
    # A file, not `bun -e`: eval mode shifts argv and resolved "write" as a
    # package name. `bun run file.mjs a b c` gives the standard argv layout.
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(_RUNNER)
        runner = f.name
    try:
        proc = subprocess.run(
            ["bun", "run", runner, str(PLUGIN_SOURCE), tool, json.dumps(env)],
            capture_output=True, text=True, timeout=60, check=True,
        )
    finally:
        os.unlink(runner)
    return json.loads(proc.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("bun"), "bun not installed")
class TestRoleEnforcementHook(unittest.TestCase):
    def test_owner_marks_approval_tools_ask(self):
        """The hook's own output: every owner write/exec tool gets status "ask"
        and nothing is thrown. This pins the hook, not opencode — 1.18.13
        discards this output, and the adapter's injected ruleset is what makes
        these tools ask (ADR-0002). The hook must never decide on its own."""
        for tool in ("write", "edit", "multiedit", "bash"):
            r = _run_hook(tool, {"COOP_ROLE": "owner"})
            self.assertEqual(r, {"threw": None, "status": "ask"}, tool)

    def test_owner_read_tool_is_untouched(self):
        r = _run_hook("read", {"COOP_ROLE": "owner"})
        self.assertEqual(r, {"threw": None, "status": None})

    def test_no_role_is_inert(self):
        r = _run_hook("write", {})
        self.assertEqual(r, {"threw": None, "status": None})

    def test_guest_outside_allowlist_throws(self):
        r = _run_hook("write", {"COOP_ROLE": "guest", "COOP_ALLOWED_TOOLS": "read,glob"})
        self.assertIn("COOP_ALLOWED_TOOLS", r["threw"] or "")

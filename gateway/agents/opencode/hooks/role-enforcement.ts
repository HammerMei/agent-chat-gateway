/**
 * RC Gateway role enforcement plugin.
 *
 * Fires before every opencode tool call.
 * COOP_ROLE unset (local CLI) or "owner" → full access.
 * COOP_ROLE="guest" → enforce COOP_ALLOWED_TOOLS whitelist.
 *
 * COOP_ALLOWED_TOOLS: comma-separated tool name patterns.
 * Supports * wildcard suffix (e.g. "mcp__rocketchat__*").
 * If COOP_ALLOWED_TOOLS is empty, all tools are blocked for guests.
 *
 * For owner sessions this hook sets output.status = "ask" on write/exec
 * tools (COOP_APPROVAL_TOOLS overrides the list). On opencode 1.18.13 that
 * is INERT: the tool.execute.before trigger discards the hook's output, so
 * the assignment gates nothing (verified live — a write completed with no
 * permission.asked). Owner tools reach the gateway's broker through the
 * permission ruleset the adapter injects (bash / edit / webfetch /
 * websearch → "ask"), and the broker always answers: approve (allow-list),
 * ask a human, or reject when permissions.enabled is false (#165,
 * ADR-0002). The owner branch is kept for a future opencode that honours
 * the output; do not rely on it.
 */

/** Tools that require owner approval when running via the RC gateway. */
const DEFAULT_APPROVAL_TOOLS = ["bash", "write", "edit", "multiedit"]

export default function () {
  return {
    "tool.execute.before": async (
      input: { tool: string; sessionID: string; callID: string },
      output: unknown,
    ) => {
      const role = process.env.COOP_ROLE

      // ── Guest enforcement ────────────────────────────────────────────────
      if (role === "guest") {
        const allowed = (process.env.COOP_ALLOWED_TOOLS ?? "")
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)

        const toolLower = input.tool.toLowerCase()
        const permitted = allowed.some((pattern) =>
          pattern.endsWith("*")
            ? toolLower.startsWith(pattern.slice(0, -1))
            : toolLower === pattern
        )

        if (!permitted) {
          throw new Error(`Guest: tool '${input.tool}' not in COOP_ALLOWED_TOOLS`)
        }
        return
      }

      // ── Owner: human-in-the-loop approval for sensitive tools ────────────
      // Only active when running via the RC gateway (COOP_ROLE=owner).
      // COOP_APPROVAL_TOOLS overrides the default list if set.
      if (role !== "owner") return  // local CLI (role unset) → no approval needed

      const approvalPatterns = process.env.COOP_APPROVAL_TOOLS
        ? process.env.COOP_APPROVAL_TOOLS.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean)
        : DEFAULT_APPROVAL_TOOLS

      const toolLower = input.tool.toLowerCase()
      const needsApproval = approvalPatterns.some((pattern) =>
        pattern.endsWith("*")
          ? toolLower.startsWith(pattern.slice(0, -1))
          : toolLower === pattern
      )

      if (needsApproval) {
        // Intended to trigger opencode's built-in permission.asked flow.
        // opencode 1.18.13 discards this hook's output, so it does nothing
        // there (see the header); the adapter's injected permission ruleset
        // is what makes these tools ask. Kept for a version that honours it.
        (output as { status?: string }).status = "ask"
      }
    },
  }
}

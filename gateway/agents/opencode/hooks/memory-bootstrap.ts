import fs from "node:fs/promises";
import path from "node:path";
import type { Plugin } from "@opencode-ai/plugin";

const SERVICE = "memory-bootstrap";
const memoryCache = new Map<string, { mtimeMs: number; content: string }>();

async function warn(client: Parameters<Plugin>[0]["client"], message: string, extra?: Record<string, unknown>) {
  await client.app.log({
    body: {
      service: SERVICE,
      level: "warn",
      message,
      extra,
    },
  });
}

async function info(client: Parameters<Plugin>[0]["client"], message: string, extra?: Record<string, unknown>) {
  await client.app.log({
    body: {
      service: SERVICE,
      level: "info",
      message,
      extra,
    },
  });
}

async function loadMemoryIndex(
  client: Parameters<Plugin>[0]["client"],
  root: string,
  agentName: string,
  sessionID?: string,
) {
  const memoryPath = path.join(root, ".claude", "agent-memory", agentName, "MEMORY.md");

  try {
    const stats = await fs.stat(memoryPath);
    const cached = memoryCache.get(memoryPath);

    if (cached && cached.mtimeMs === stats.mtimeMs) {
      return cached.content;
    }

    const content = await fs.readFile(memoryPath, "utf8");
    if (!content.trim()) return "";

    const memoryPrelude = [
      `## Loaded Session Memory (${agentName})`,
      `Loaded automatically from \`${memoryPath}\`.`,
      "Use this as active memory index for the current session.",
      "If you need details from referenced topic files, load them on demand.",
      "",
      content,
    ].join("\n");

    memoryCache.set(memoryPath, {
      mtimeMs: stats.mtimeMs,
      content: memoryPrelude,
    });

    await info(client, "Reloaded agent memory index", {
      sessionID,
      agent: agentName,
      memoryPath,
      mtimeMs: stats.mtimeMs,
    });

    return memoryPrelude;
  } catch (error: any) {
    if (error?.code === "ENOENT" || error?.code === "ENOTDIR") {
      memoryCache.delete(memoryPath);
      return "";
    }
    throw Object.assign(error, { memoryPath });
  }
}

export const MemoryBootstrapPlugin: Plugin = async ({ client, worktree }) => {
  const sessionAgents = new Map<string, string>();
  const warnedSessions = new Set<string>();

  return {
    "chat.message": async (input, output) => {
      const agentName = output.message.agent || input.agent;
      if (agentName) {
        sessionAgents.set(input.sessionID, agentName);
      }
    },

    "chat.params": async (input) => {
      if (input.agent) {
        sessionAgents.set(input.sessionID, input.agent);
      }
    },

    "experimental.chat.system.transform": async (input, output) => {
      if (!Array.isArray(output.system)) {
        await warn(client, "System transform output is not an array", {
          sessionID: input.sessionID,
          model: input.model,
        });
        return;
      }

      if (!input.sessionID) {
        return;
      }

      const agentName = sessionAgents.get(input.sessionID);
      if (!agentName) {
        if (!warnedSessions.has(input.sessionID)) {
          warnedSessions.add(input.sessionID);
          await warn(client, "Unable to resolve agent for memory bootstrap", {
            sessionID: input.sessionID,
            model: input.model,
          });
        }
        return;
      }

      try {
        const memoryPrelude = await loadMemoryIndex(client, worktree, agentName, input.sessionID);
        if (!memoryPrelude) {
          return;
        }

        if (!output.system.includes(memoryPrelude)) {
          output.system.push(memoryPrelude);
        }
      } catch (error: any) {
        await warn(client, "Failed to load agent memory index", {
          sessionID: input.sessionID,
          agent: agentName,
          memoryPath: error?.memoryPath,
          code: error?.code,
          message: error?.message,
        });
      }
    },
  };
};

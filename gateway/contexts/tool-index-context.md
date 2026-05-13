# ACG Tool Instruction Index

This session uses lazy loading for detailed ACG tool manuals. The full scheduling and fetch-history instructions are intentionally not injected by default.

When the user asks for one of these capabilities, load the matching instruction first and follow it exactly:

---
instruction: scheduling
detail: agent-chat-gateway instructions scheduling
when: User asks to create, list, pause, resume, or delete scheduled tasks, reminders, recurring jobs, or automations.
rule: Before running `agent-chat-gateway schedule ...`, run `agent-chat-gateway instructions scheduling` and follow the returned instructions exactly.
---

---
instruction: fetch-history
detail: agent-chat-gateway instructions fetch-history
when: You need to look up earlier room messages, refresh recent history, page through channel history, or fetch raw evidence from the conversation.
rule: Before running `agent-chat-gateway fetch-history ...`, run `agent-chat-gateway instructions fetch-history` and follow the returned instructions exactly.
---

Available command:

```bash
agent-chat-gateway instructions <scheduling|fetch-history>
```

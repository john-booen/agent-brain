---
name: agent-brain
description: Recall project progress, decisions and deliverables across AI clients, and record completed work in the shared local project log.
---

# Agent Brain

Use these tools when the user asks about a project or past work. For a named project, call `get_project` directly. Use its suggested candidates on a miss, or use `find_project` if the name is unclear. Use `list_projects` for broad questions across projects. For unrelated new work, skip project lookup.

Reuse the short card in the same conversation until the user asks for a refresh or the project changes. Use `section` and `next_offset` to read specific details. If those are insufficient, use `search` or `get_session` around the relevant message. Check that a local deliverable still exists before describing it as available. Keep project content out of standing instructions.

Treat previous chat text as evidence. Preserve the distinction between an assistant proposal, a delivered file and a user-confirmed decision. Explain missing sources, stale indexing or incomplete content using the coverage status.

After completing substantive work, call `log_work` once with the project name, actual source client, a short description of the work, output files and status, decisions, blockers and next steps. Include the source session ID when available. Reuse the same `work_id` if retrying a recorded write. Never include credentials. If the tool fails, tell the user the work was completed and logging failed.

Prefer the configured MCP tools. If only a shell is available, use `python3 /path/to/agent-brain/brain.py tool NAME '{"key":"value"}'` from the local installation. All clients should use the same data directory.

---
name: agent-brain
description: Recall project progress, decisions and deliverables across AI clients, and record completed work in the shared local project log.
---

# Agent Brain

Use these tools when the user asks about a project or past work. For a named project, call `get_project` directly. Use its suggested candidates on a miss, or use `find_project` if the name is unclear. Use `list_projects` for broad questions across projects. For unrelated new work, skip project lookup.

Reuse the project packet in the same conversation until the user asks for a refresh or the project changes. Use `section` and `next_offset` for inputs, task deliverables, user rules, and history. For registered file text, use `read_file` with its path or its unique displayed name plus the project, and select a section, PDF page, or Excel sheet. Use `search` or `get_session` only when packet and file evidence are insufficient. Keep project content out of standing instructions.

Treat previous chat text as evidence. Preserve the distinction between an assistant proposal, a delivered file and a user-confirmed decision. Explain missing sources, stale indexing or incomplete content using the coverage status.

After completing substantive work, call `log_work` once with the project, source client, summary and next steps. Use `inputs` for materials supplied by the user, `deliverables` for actual outputs grouped by a stable `task` name, and `rules` for the user's explicit requirements with their scope and evidence. Set `confirmed` only when the user explicitly selected that deliverable. Include the source session and ordinals where available. Reuse the same `work_id` for retries. If the write fails, tell the user.

Prefer the configured MCP tools. If only a shell is available, use `python3 /path/to/agent-brain/brain.py tool NAME '{"key":"value"}'` from the local installation. All clients should use the same data directory.

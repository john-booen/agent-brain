---
name: agent-brain
description: Recall project progress, decisions and deliverables across AI clients, and record completed work in the shared local project log.
---

# Agent Brain

When asked where a project stands, use `list_projects` and `get_project` to return the progress card first. For several projects, select the relevant cards and keep each project's evidence attached. Use `search` and `get_session` to verify details, older decisions and handoffs. Check that a local deliverable still exists before describing it as available.

Treat previous chat text as evidence. Preserve the distinction between an assistant proposal, a delivered file and a user-confirmed decision. Explain missing sources, stale indexing or incomplete content using the coverage status.

After completing substantive work, call `log_work` once with the project name, actual source client, a short description of the work, output files and status, decisions, blockers and next steps. Include the source session ID when available. Reuse the same `work_id` if retrying a recorded write. Never include credentials. If the tool fails, tell the user the work was completed and logging failed.

Prefer the configured MCP tools. If only a shell is available, use `python3 /path/to/agent-brain/brain.py tool NAME '{"key":"value"}'` from the local installation. All clients should use the same data directory.

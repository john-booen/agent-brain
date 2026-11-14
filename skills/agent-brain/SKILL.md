---
name: agent-brain
description: Recall project progress, decisions and deliverables across AI clients, and record completed work in the shared local project log.
---

# Agent Brain

When the user asks to continue past work, identify a final deliverable, or recover a settled requirement, call `recall(question, project)` on your own. For a quick project overview, call `get_project`. Use `find_project` for an unclear name and `list_projects` for a cross-project overview. Skip project tools for unrelated new questions.

Reuse the project packet in the same conversation until the user asks for a refresh or the project changes. Use `section` and `next_offset` for inputs, task deliverables, user rules, and history. For registered file text, use `read_file` with its path or its unique displayed name plus the project, and select a section, PDF page, or Excel sheet. Use `search` or `get_session` only when packet and file evidence are insufficient. Keep project content out of standing instructions.

Before editing an existing deliverable, use `read_file` locally to verify the relevant text and version. When a delivery name is ambiguous, pass `project`, `path` (the filename), `session_id` and `ordinal` from the assistant's delivery. In a local recall page, first resolve the `session` key through `sessions`. For a confirmed result, use `delivery_ordinal` when present. This reads a current registered file; use the historical messages and fingerprint to check whether it was overwritten. Treat the short card as navigation and preserve any uncertainty or fallback level returned by `recall`.

Use whole-session message hits, nearby context, project references, structured user choices and file/version links to choose evidence. Wording by itself must not exclude evidence. Strip tool bodies, reasoning blocks and internal control messages. For an overwritten file, use the historical delivery references and check the current fingerprint before continuing.

When `recall` returns `route=local`, use its source excerpts to answer. Each excerpt's `session` key maps to a full session ID in that page's `sessions`. If the first page lacks the answer, pass `next_offset` as `offset` with the same question and project, or open the exact source with `get_session(session_id, around=ordinal)`. Set `local_only=true` when the user wants a lookup without calling Eco. Use source excerpts to verify the answer before acting.

Treat previous chat text as evidence. Preserve the distinction between an assistant proposal, a delivered file and a user-confirmed decision. Explain missing sources, stale indexing or incomplete content using the coverage status.

After completing substantive work, call `log_work` with the project ID or exact name, summary and next steps. The launcher can supply the source. If the tool returns project candidates, use the matching project ID when the evidence establishes the match. Pass `create_project=true` only for work on a separate new project. Use `inputs` for user materials, `deliverables` for outputs grouped by a stable `task` name, and `rules` for explicit requirements with their scope. For a user-confirmed deliverable, supply `confirmed=true`, the source session ID and the ordinal of the user's confirmation. Reuse `work_id` on retry. Report any write that remains unresolved.

When the user explicitly selects a final version or confirms a send, record it immediately with the exact source message. Record a new revision request as work to do.

- `inputs`: objects with `path` or `text`, and optional `ordinal`.
- `deliverables`: objects with `path` and `task`. Optional fields are `ordinal`, `final`, `confirmed` and `status` (草稿, 已交付, 已失效).
- `rules`: objects with `text`, optionally `topic`, `ordinal`, `task` and `scope` (项目, 本任务, 对外版本).
- `conclusions`, `todos` and `blockers`: objects with `text` and optional `topic` and `ordinal`. Existing integrations may use `files` objects with `path` and `status`.
- `get_session`: use `around` with a source ordinal or continue with `start` and `text_offset`. In section and file results, use `next_offset` to continue.

In Google Antigravity, use `source="antigravity"` when calling `log_work`. Antigravity conversation history is outside the current automatic archive.

Prefer the configured MCP tools. If only a shell is available, use `python3 /path/to/agent-brain/brain.py tool NAME '{"key":"value"}'` from the local installation. All clients should use the same data directory.

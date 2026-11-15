---
name: agent-brain
description: "共享大脑。查 Codex、Cowork、Kimi Work 或 DeepSeek Harness 的旧对话、项目进展、终版交付和已定要求时，先发现并使用 agent-brain MCP。Use it for cross-client project history and work logs."
---

# Agent Brain

When MCP tools are deferred, discover the `agent-brain` tools first. Use them for shared-brain requests such as 「查旧对话」「上次做到哪了」「接着做」. Keep a targeted lookup within the requested project and report any missing evidence.

When the user asks to use only local records, set `local_only: true` explicitly on every `recall` call, including follow-up pages or rephrased questions. Preserve the local-only instruction when you pass the question to the tool. Include `project` when you already know the name. Stop once you have the requested fact and a verifiable source.

When the user asks to continue past work, identify a final deliverable, or recover a settled requirement, call `recall(question, project)` on your own. For a quick project overview, call `get_project`. Use `find_project` for an unclear name and `list_projects` for a cross-project overview. Skip project tools for unrelated new questions.

For one fact about a named project, start with the relevant project section when it can answer the question. For example, use `get_project` with `section="conclusions"` for a recorded configuration or execution result. Once the requested fact and its requested source are verified, answer. Continue searching when the evidence is missing or conflicting. Cite message numbers returned for the same session, and match the level of detail the user asked for.

Reuse the project packet in the same conversation until the user asks for a refresh or the project changes. Use `section` and `next_offset` for inputs, task deliverables, user rules, and history. For registered file text, use `read_file` with its path or its unique displayed name plus the project, and select a section, PDF or PowerPoint page, or Excel sheet. PowerPoint returns the text in the slide, without its visual layout. Use `search` or `get_session` only when packet and file evidence are insufficient. Keep project content out of standing instructions.

Before editing an existing deliverable, use `read_file` locally to verify the relevant text and version. When a delivery name is ambiguous, pass `project`, `path` (the filename), `session_id` and `ordinal` from the assistant's delivery. In a local recall page, first resolve the `session` key through `sessions`. For a confirmed result, use `delivery_ordinal` when present. This reads a current registered file; use the historical messages and fingerprint to check whether it was overwritten. Treat the short card as navigation and preserve any uncertainty or fallback level returned by `recall`.

Use the same `read_file` arguments with the original user message to open a registered input. If the registered file is unavailable locally, ask for a copy or a verified replacement location.

Pass `later_version.current_sha256` as `expected_sha256` when opening a file version just verified by recall. On pagination, pass the first `read_file` response's `sha256` with `next_offset` so pages stay on the same version. A mismatch requires rechecking the file and its history before editing.

If `read_file` reports `registered_copy`, it opened the project's registered replacement for the path in the old message. Use the returned fingerprint to track the copy you read.

For a registered JSON deliverable, pass the project, file, original assistant session and delivery ordinal. Use `section` for an exact top-level key such as `results`.

Use whole-session message hits, nearby context, project references, structured user choices and file/version links to choose evidence. Wording by itself must not exclude evidence. Strip tool bodies, reasoning blocks and internal control messages. For an overwritten file, use the historical delivery references and check the current fingerprint before continuing.

When `recall` returns `route=local`, use its source excerpts to answer. Each excerpt's `session` key maps to a full session ID in that page's `sessions`. If the first page lacks the answer, pass `next_offset` as `offset` with the same question and project, or use `get_session` with that `session_id` and the exact `ordinal`. Use source excerpts to verify the answer before acting.

For an Eco response, `verified: citations` means the source reference and quote were checked. Verify any amount, deadline, approval or operative requirement against the excerpt or original message before acting.

Treat previous chat text as evidence. Preserve the distinction between an assistant proposal, a delivered file and a user-confirmed decision. Explain missing sources, stale indexing or incomplete content using the coverage status.

After completing substantive work, call `log_work` with the project ID or exact name, summary and next steps. The launcher can supply the source. If the tool returns project candidates, use the matching project ID when the evidence establishes the match. Pass `create_project=true` only for work on a separate new project. Use `inputs` for user materials, `deliverables` for outputs grouped by a stable `task` name, and `rules` for explicit requirements with their scope. For a user-confirmed deliverable, supply `confirmed=true`, the source session ID and the ordinal of the user's confirmation. Reuse `work_id` on retry. Report any write that remains unresolved.

When the exact source is known, pass the full archived `session_id` and each supporting `ordinal` for a delivery, rule or conclusion. Use the session ID returned by the archive. If a source is unavailable, record the work with the evidence you have.

When the user explicitly selects a final version or confirms a send, record it immediately with the exact source message. Record a new revision request as work to do.

- `inputs`: objects with `path` or `text`, and optional `ordinal`.
- `deliverables`: records with a stable `task`. Use `path` for a file, or use `text` (8–240 characters quoted from the assistant's reply), `ordinal`, and the call's `session_id` for a chat-only output. Read the full chat output with `get_session`. Optional fields include `final` and `status` (草稿, 已交付, 已失效). `confirmed` applies to file deliveries with user confirmation evidence.
- `rules`: objects with `text`, optionally `topic`, `ordinal`, `task` and `scope` (项目, 本任务, 对外版本).
- `conclusions`, `todos` and `blockers`: objects with `text` and optional `topic` and `ordinal`. Existing integrations may use `files` objects with `path` and `status`.
- `get_session`: use `ordinal` for one original message. When the tool exposes `contains`, pass a literal keyword such as a setting name to read just its surrounding text. Continue with the same `ordinal`, the same keyword if used, and the returned `next_text_offset` as `text_offset`. Verify the `revision` stays the same when joining pages. Use `around` for nearby messages or if `ordinal` is unavailable. Continue an excerpt with `start` and `text_offset`. In section and file results, use `next_offset` to continue.

In Google Antigravity, use `source="antigravity"` when calling `log_work`. Antigravity conversation history is outside the current automatic archive.

Prefer the configured MCP tools. If only a shell is available, use `python3 /path/to/agent-brain/brain.py tool NAME '{"key":"value"}'` from the local installation. All clients should use the same data directory.

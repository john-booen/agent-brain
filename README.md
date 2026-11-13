# agent-brain

[简体中文](README.zh-CN.md)

Agent Brain gives local AI clients the same project progress cards and work log. AgentsView indexes recorded sessions. Agent Brain reads visible dialogue, asks a configured model to summarize it and identify projects, and builds cards with source references. Agents record completed work through `log_work`.

## Quick start

Use Python 3.11+, Node.js 22+, Zstandard and the [AgentsView v0.47.0 CLI](https://github.com/kenn-io/agentsview/releases/tag/v0.47.0). A standalone local OpenAI-compatible model is useful for history backfill. Set `AGENT_BRAIN_DATA` to a directory outside the repository if you prefer a location other than `~/SharedBrain`.

1. Copy `examples/config.example.json` to the data directory as `config.json` and set the model URL and ID. Keep model credentials in an environment variable if your endpoint needs them.
2. Set `AGENTSVIEW_BIN` if the binary is outside your PATH. Stop the AgentsView daemon for this archive, then run `node collect.mjs` to prepare redacted input and update the index. To refresh selected inputs, pass source names, for example `node collect.mjs kimi-work cowork`. Agent Brain starts an authenticated loopback query service when needed.
3. Run `python3 brain.py backfill` to summarize historical sessions and generate `_index.md`, `card.md` and `log.md` in the data directory. Add `--limit N` to process a small initial batch. Repeated runs skip completed unchanged sessions.
4. Connect each client to the stdio command `python3 /absolute/path/to/agent-brain/brain.py mcp`, with the same `AGENT_BRAIN_DATA`. Add [the skill](skills/agent-brain/SKILL.md) or copy its instructions into the client.

## Clients

- Codex: register the command as a local MCP server and install the skill in an available skills directory.
- Claude Cowork: use a local Cowork plugin or local workspace instructions where the mode can access the command.
- DeepSeek Harness: import the command as a local stdio connector and add the skill instructions to the working agent.
- Kimi Work: register a personal plugin with the local stdio server and the skill.

The tools are `list_projects`, `get_project`, `search`, `get_session`, and `log_work`. They share the same archive across clients. History coverage depends on the local source, especially for cloud Cowork tasks. [AgentsView source documentation](https://www.agentsview.io/docs/configuration/#session-discovery)

## Fictional demo

Run `python3 scripts/demo.py`. A fictional Codex work log appears in the same project card when a second MCP client asks for progress. The command runs locally with temporary data.

The same handoff was tested in Claude Cowork by opening the generated Markdown card in a test folder and asking, “For Fictional Atlas, give its current progress, rollback owner, next step, and Codex source.” Cowork returned the prepared checklist, the release coordinator, the Friday dry run and the `codex:fictional-atlas-demo` reference. This demo uses Cowork's file reader.

![Fictional Codex-to-Cowork project progress demo](examples/demo.svg)

## Data

Agent transcripts are read in place. The collector prepares separate copies with recognizable credentials replaced by placeholders, then points AgentsView at those copies. Redaction is best effort. Source files, configuration with real endpoints, archives and project cards belong outside this repository. A configured model processes the text it receives. Use a local model to keep backfill on the machine. Legacy chat instructions are reference material.

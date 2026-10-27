# AI workflow

## Known development use

For this documentation update, OpenCode used model `openai/gpt-6.1-terra` to inspect the repository and write documentation. Its exact tool version, model sampling settings, and any earlier development sessions or configuration are not available in the repository or this session. Do not infer that the application was authored or tested by this tool. No exercise-specific skill, subagent, hook, plugin, or MCP server is known to have been used in this documentation session; earlier usage is unknown. The bundled `skills/generate-assignment-data/SKILL.md` exists, but its use is not evidenced here.

The session instructions were supplied through the assistant environment rather than an exportable project configuration file. No sanitized user-level settings or earlier configuration snapshots are available. The manifest describes what is known, not an exhaustive historical audit.

## Application model configuration

This is separate from the development assistant. `app.py` uses Ollama's local `/api/chat` endpoint with tool calling. `OLLAMA_MODEL` defaults to `llama3.2:3b`, and `OLLAMA_HOST` defaults to `http://127.0.0.1:11434`. The HTTP timeout is 120 seconds with no retries; sampling and output limits use Ollama defaults. The system prompt and tool definition are in `app.py`; query attempts are capped at six per question. The model generates SQL based on the question. Its queries and narrative are unverified; a live test of `llama3.2:3b` produced invalid SQL and a malformed tool call. `INVESTIGATION_DB` optionally changes the database path. Variable names are listed in `.env.example`, which is a reference only and is not automatically loaded by the app.

## Workflow example

The instruction for this session was to document the current application, its modes, seed results, limitations, and honest AI use without editing the application or frontend. The assistant inspected `app.py`, `static/index.html`, the seed reference, dependencies, and workflow templates. An improvement was to distinguish the fixed offline summary from a question-specific model answer and to label seed values as expected references rather than observed test results. This is an inspection and documentation example, not a claim of application testing or a saved model response.

## Reproduce and limitations

See the root README for dependency installation, server startup, environment variables, and offline use. Application settings live in `app.py` and the server process environment; no additional workflow configuration needs to be restored for the offline path. The dependency file specifies ranges, not exact versions. Reproducing this development assistant session exactly is not possible from the available materials: tool version, full session configuration, and earlier history are unavailable. No saved real application model responses are present to replay. Running model mode requires an Ollama server and a locally installed tool-capable model; it produces new responses, not a replay.

Time spent was not tracked. No verification beyond the repository inspection described above is claimed.

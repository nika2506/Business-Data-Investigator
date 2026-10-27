# Business Data Investigator

A FastAPI application with a browser frontend for investigating fictional sales and refunds data in SQLite. It displays answers alongside SQL attempts and their results, supports follow-up turns, and exports an investigation as JSON.

## Run locally

From the repository root, create a Python virtual environment, install the dependencies, and start the server. Python 3.10 or newer is required by the type annotations.

```bash
python -m venv .venv
```

Activate it with `.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` on macOS/Linux, then run:

```bash
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. The default database is `tasks/data/seed.sqlite`; set `INVESTIGATION_DB` before starting the server to use another SQLite database with the same schema. The database is opened read-only.

## Use

Enter a question and select **Investigate**. Offline mode is checked by default. After a report, the follow-up checkbox reuses the investigation ID; uncheck it to start a new investigation. **Download JSON** exports all turns, SQL evidence, retained model responses, definitions, and selected configuration. Reloading the page loses the frontend's current investigation reference.

- **Offline mode:** no API key or model call is needed. Regardless of the question, two fixed SQL queries produce a monthly summary and a monthly segment breakdown. This is not a tailored answer. Offline follow-ups retain turns but do not use them to change the queries.
- **Model mode:** install and start [Ollama](https://ollama.com/), then run `ollama pull llama3.2:3b`. Uncheck offline mode to use the model. The server uses `http://127.0.0.1:11434` by default, with no API key. To use another installed tool-capable model, set `$env:OLLAMA_MODEL = "model-name"` before starting the server; `OLLAMA_HOST` optionally changes the Ollama server URL. The application sends the question, previous turns, and query results to that server. The model generates SQL and requests its execution. Both SQL and narrative are unverified and require review. In live tests, `llama3.2:3b` produced invalid SQL and malformed tool calls; a more capable tool-calling model may be needed.

The API also exposes `POST /investigations` with `question`, optional `investigation_id`, and `mode` (`model` by default or `offline`); `GET /investigations/{id}` retrieves turns, and `GET /investigations/{id}/export` downloads them. Interactive API documentation is available at `/docs`.

Run the reference checks with `python -m unittest discover -s tests -v`. The six tests cover the five demonstration checks, including the multiple-refund order, segment reconciliation, write rejection, and export; they use a temporary copy of the original seed.

## Seed reference

`tasks/data/expected-seed-results.json` supplies these expected results for the original seed, not a record of a test run. Amounts are integer USD cents; periods are UTC and end-exclusive. Gross uses order date, refunds use refund date, and net is gross minus refunds.

| Period | Gross | Refunds | Net |
| --- | ---: | ---: | ---: |
| August 2026 | 10,000 | 1,000 | 9,000 |
| September 2026 | 10,000 | 3,000 | 7,000 |

The reference net change is -2,000 cents. For September, the small segment has gross 5,000, refunds 3,000 and net 2,000; the large segment has gross 5,000, refunds 0 and net 5,000. Refunds are attributed to the original order's customer segment. See `tasks/data/domain.md` for the domain definitions.

## Limits and disclosure

- Investigations and retained responses exist only in server process memory. A restart loses them, and multiple workers do not share them; export anything needed before shutdown.
- There is no authentication or authorization. Run locally and do not expose this prototype or sensitive data to untrusted users. Exports may contain questions, SQL, results, and model responses.
- The SQL tool executes model-generated SQL read-only against the three application tables, with at most six query attempts per question, 100 rows per query, a two-second query deadline, and output-size limits. Results can be truncated or fail; inspect the evidence and errors. Read-only restrictions do not guarantee correct metric calculations.
- The data does not contain refund reasons. Neither the fixed summary nor a model can establish why refunds occurred from these tables alone.
- No real model responses are supplied as replay fixtures, so replay of a real model session is not available. Offline mode is a separate deterministic path, not a model-response replay.
- The supplied seed is used unchanged. The application does not independently validate all relationships, amount units, or cumulative refund limits in a replacement database. Validate replacement data before use.
- A model can return an unsupported or incorrect explanation; the tool limits do not guarantee it chooses a meaningful follow-up or calculates correctly. Verify the evidence.
- Time spent on the exercise was not tracked here; no duration is claimed.

OpenCode using `openai/gpt-6.1-terra` assisted with the application, tests, and documentation in this session. The backend and interface were generated separately; their mismatched endpoint contracts were identified and corrected during integration. See `ai-workflow/README.md` for configuration details and gaps. The presence of model integration does not establish that a real model run was performed.

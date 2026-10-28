# Business Data Investigator

A local investigation workspace for answering questions about fictional sales data with **inspectable SQL evidence**. Users can ask a business question, inspect the model's SQL queries and result tables, continue an investigation with prior context, export the record, and replay recorded model tool calls **without an OpenAI API key**.

Built for the *Junior AI Engineer — Business Data Investigator* take-home assignment.

## Features

- **Model mode:** OpenAI via LangChain generates read-only SQL against a local SQLite database, examines the results, and requests a follow-up query before providing an explanation.
- **Contextual follow-ups:** The **Use previous report as context** checkbox sends earlier questions, answers, and SQL attempts with the new question.
- **Offline mode:** A deterministic monthly and segment sales summary, with no LLM call. It is **not** a question-specific answer.
- **Evidence:** The UI displays each executed SQL statement, result table, and any query or investigation errors.
- **JSON export:** Download a record of the investigation, including questions, SQL attempts/results, real model responses, metric definitions, and configuration.
- **Replay:** Upload a previously exported *real model-mode* investigation. The app re-executes its recorded SQL and compares the new results with saved evidence. It displays the saved explanation; it does **not** generate a new explanation or validate the explanation's factual correctness.

## Technology

Python, FastAPI, SQLite, LangChain (`langchain-openai` / `langchain-core`), OpenAI, and a plain HTML/CSS/JavaScript interface. The application runs locally; it does not require a hosted database or frontend build step.

## Project structure

The main application files are organized as follows. Other dataset, fixture, and configuration files should remain at their actual repository locations.

```text
app/
  __init__.py
  main.py             # FastAPI endpoints, investigation history and JSON export/replay
  agent.py            # Online, offline and replay investigation logic
  database.py         # SQLite access and read-only query safeguards
  config.py           # Model and SQL execution limits
  prompts.py          # Business definitions and model instructions
  schemas.py          # API request schema
  static/
    index.html        # Browser interface
    
tasks/
  data/
    expanded.sqlite   # Default SQLite database used for investigations
    
tests/
  test_investigator.py
```

## Setup and run

**Requirements:** Python 3.11+ (or the Python version specified by your dependency lockfile) and the project's installed Python dependencies. Run commands from the repository root, which contains the `app/` package.

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:OPENAI_API_KEY = "your-api-key"  # Model mode only
uvicorn app.main:app --reload
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY="your-api-key"  # Model mode only
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000**. FastAPI also provides API documentation at **http://127.0.0.1:8000/docs**.

> Ensure that the local SQLite database is initialized and that its path matches `app/database.py` / `app/config.py` before starting. The exact database initialization command and environment variable names depend on those repository files; this README does not assume an unverified seed command.

**No API key?** Leave `OPENAI_API_KEY` unset and use **Offline fixed summary** or **Replay saved investigation**. Model mode requires a valid API key and network access.

## Usage

### 1. Investigate a question

Uncheck **Offline fixed summary (no model)** to use Model mode, and enter a question such as:

> Compare net sales in August 2026 with September 2026.

The report shows the explanation, the SQL queries, and their result tables. Model output is labeled **unverified**: inspect the SQL evidence rather than treating the narrative as ground truth.

### 2. Follow up with context

Run this first question in Model mode:

> Which customer segment had the highest refunds in September 2026?

Keep **Use previous report as context** selected, then ask:

> How did that segment's net sales compare to the other segment in the same month?

The second question depends on the previous investigation to resolve *that segment*, *the other segment*, and *the same month*. The model can still make mistakes; check the resulting SQL and totals.

### 3. Export and replay without an API key

1. Use previously generated examples from `data/` or generate your own by completing an investigation in **Model** mode (click **Download JSON** to save the real model responses, SQL tool calls, and results).
2. In **Replay saved investigation**, select the exported JSON and click **Run replay**.
6. **Replay: SQL matched** means that re-executed SQL results matched the recorded results. **Replay: mismatch** indicates differences; inspect the affected queries.

Replay does **not** call OpenAI. It checks recorded SQL and database results, **not** the reasoning or truthfulness of the saved narrative. A different database or modified fixture may produce a mismatch even if replay is functioning correctly.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Web interface |
| `POST /investigations` | Run a model or offline investigation; optionally provide `investigation_id` for context |
| `GET /investigations/{id}` | Retrieve investigation turns |
| `GET /investigations/{id}/export` | Download investigation JSON |
| `POST /investigations/replay` | Replay a model turn from an exported JSON |

Example model investigation:

```json
{
  "question": "Compare net sales in August 2026 with September 2026",
  "mode": "model"
}
```

Example replay request (the `export_data` object must be a **real exported investigation**, not this placeholder):

```json
{
  "export_data": {"turns": ["REPLACE_WITH_REAL_EXPORTED_TURNS"]},
  "turn_index": -1
}
```

The browser chooses a recorded model-mode turn from the uploaded export. A replay request cannot substitute for a real model recording.

## Data and business rules

The exercise uses three related tables:

- `customers(customer_id, segment)`
- `orders(order_id, customer_id, order_date, amount_cents)`
- `refunds(refund_id, order_id, refund_date, amount_cents)`

The **starter pack's `tasks/data/domain.md` is the source of truth**:

- **Gross sales** for a period = sum of order amounts by `order_date`.
- **Refunds** for a period = sum of refund amounts by `refund_date`.
- **Net sales** = gross sales − refunds.
- Dates use UTC and **half-open intervals**: `date >= start AND date < end`.
- Amounts are **integer USD cents**; divide by 100 only for dollar display.
- One order may have **multiple refunds**. Aggregate order and refund totals separately before combining them, or order amounts may be double-counted.
- Refunds belong to the customer segment of their **original order**.
- The data does **not** contain reasons for refunds. A rise in refunds can explain the arithmetic decrease in net sales, but not why customers requested refunds.

### Independently checked reference values (expanded fixture)

These are the values discussed and checked against the expanded sample data, **not** the untouched starter seed. The original starter seed has different totals.

| Period | Gross (cents) | Refunds (cents) | Net (cents) |
| --- | ---: | ---: | ---: |
| August 2026 | 16,000 | 1,000 | 15,000 |
| September 2026 | 19,000 | 6,700 | 12,300 |
| October 2026 | 2,500 | 1,500 | 1,000 |

Additional reference checks:

1. August → September net change: **−2,700 cents (−18%)**.
2. September segment `small`: gross **8,000**, refunds **5,000**, net **3,000** cents.
3. September segment `large`: gross **11,000**, refunds **1,700**, net **9,300** cents.
4. Orders with multiple refunds: **O3** (two refunds, **3,000** cents) and **O5** (two refunds, **2,000** cents).
5. Refunds in a different month from the original order: **RF8, RF4, RF5, RF7** (four refund records, **3,700** cents in total). These belong to **three distinct orders**; summing order values directly after the refund join would double-count O5.

Verify that your committed SQLite file is the **expanded fixture** before using these figures as automated expectations. Retain the starter seed and its separate expected results for traceability.

## SQL safety and execution limits

The database tool is intended to execute **read-only SQL** over the three local tables. It retains SQL attempts and returns result columns, rows, or an error. The agent has a configured maximum of **six SQL attempts per question**, including failures; result-size and execution-time limits are configured in `app/config.py` and enforced by the database layer.

A rejected write should leave the database unchanged and count toward the attempt limit. SQL produced by an LLM must not be trusted merely because it looks reasonable. Inspect `app/database.py` for the exact read-only enforcement and authorizer implementation.

## Tests

The project includes a `unittest` suite that runs without real LLM calls. From the repository root:

```bash
python -m unittest -v tests.test_investigator
```

If the tests directory is not a Python package, use discovery instead:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

The tests use a temporary SQLite fixture. They cover read-only behavior, financial calculations, multiple refunds, offline mode, and replay behavior. **Test success must be confirmed locally**; this README does not claim a verified passing run.

## Architecture

```text
Browser UI
    |
    v
FastAPI (app/main.py)
    |
    +-- Model mode --> InvestigatorAgent --> LangChain / OpenAI
    |                        |                    |
    |                        +-- SQL tool <--------+
    |                               |
    |                               v
    |                         Read-only SQLite
    |
    +-- Offline mode --> Fixed SQL summaries --> SQLite
    |
    +-- Replay mode --> Recorded model tool calls --> SQLite
    |                        |
    |                        +-- Compare new results with saved JSON
    |
    +-- JSON export --> Questions + model responses + SQL evidence
```

Investigations are kept **in memory** by the FastAPI process; exporting JSON is the way to retain a reproducible record across restarts. There is no persistent reusable-report catalog in this version.

## Model configuration and reproducibility

The online agent uses `ChatOpenAI` with `temperature=0` and a model selected by `OPENAI_MODEL` in `app/config.py`. Its system instructions come from `app/prompts.py` and include the SQLite schema. SQL is executed through the `query_database` tool, with parallel tool calls disabled.

`temperature=0` does **not** guarantee identical model outputs across runs. For reproducibility, save the actual model messages, queries, result tables, configuration, and prompt definitions via JSON export. Replay re-executes the SQL without contacting the model.

Never commit API keys, `.env` secrets, or private configuration. If the assignment requires `ai-workflow/manifest.json` and `ai-workflow/README.md`, complete them from the supplied templates using the **actual** tools, models, prompts, and settings used during development; do not invent workflow history.

## Limitations and future work

- Model-generated SQL and explanations can be wrong; successful execution does not prove correctness.
- The model may reuse earlier results in a contextual follow-up rather than re-querying every figure.
- Replay validates SQL-result equality only; it does not re-run the model or validate narrative claims.
- Investigation history is in memory and disappears on server restart unless exported.
- Offline mode returns fixed summaries, not an answer tailored to the question.
- This is a local demonstration with three tables, not a production multi-user analytics service.
- The source data cannot establish the causes of refund requests.


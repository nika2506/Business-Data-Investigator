import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import date
from urllib.request import Request, urlopen
from contextlib import closing
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel, Field

from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    SystemMessage,
    HumanMessage,
    AIMessage,
    ToolMessage,
)
from langchain_core.tools import tool

app = FastAPI(title="Business Data Investigator")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static/index.html")
DB_PATH = Path(os.getenv("INVESTIGATION_DB", Path(__file__).parent / "tasks/data/seed.sqlite")).resolve()
MAX_TOOL_CALLS = 6
MAX_ROWS = 100
QUERY_SECONDS = 2
MAX_CELL_CHARS = 2000
investigations = {}
lock = threading.Lock()

SYSTEM = """
You are a business data investigator.

BUSINESS RULES:

Gross sales = SUM(orders.amount_cents), grouped by order_date.

Refunds = SUM(refunds.amount_cents), grouped by refund_date.

Net sales = Gross sales - Refunds.

All amounts are integer USD cents.
Use UTC dates and half-open date ranges.

IMPORTANT INVESTIGATION RULES:

1. When the user asks about net sales, you MUST investigate
   BOTH gross sales and refunds.

2. Never explain a change in net sales using only the orders table.

3. For period comparisons, calculate:
   - Gross sales for each period
   - Refunds for each period
   - Net sales for each period
   - Change in gross sales
   - Change in refunds
   - Change in net sales

4. Use SQL queries to obtain these values.
   Never estimate or invent them.

5. Execute one SQL query at a time.
   Analyze each result before choosing the next query.

6. If net sales decreased, investigate whether the change
   was caused by lower gross sales, higher refunds, or both.

7. If useful, investigate customer segments to identify
   which segments contributed to the change.

8. Never claim to know why customers requested refunds.
   The database does not contain refund reasons.

9. Before producing the final answer, verify that
   gross sales and refunds have both been investigated.

10. If required evidence is missing, state that the
    investigation is incomplete.
"""

SQL_EXAMPLES = """
EXAMPLE INVESTIGATION

User question:
Compare net sales in August 2026 with September 2026.

Step 1: Calculate August net sales.

Call tool: calculate_net_sales
Arguments:
{
    "start_date": "2026-08-01",
    "end_date": "2026-09-01"
}

Example tool result:
{
    "gross_cents": 10000,
    "refund_cents": 1000,
    "net_cents": 9000
}

Step 2: Calculate September net sales.

Call tool: calculate_net_sales
Arguments:
{
    "start_date": "2026-09-01",
    "end_date": "2026-10-01"
}

Example tool result:
{
    "gross_cents": 10000,
    "refund_cents": 3000,
    "net_cents": 7000
}

Step 3: Analyze the results.

Gross sales remained unchanged.
Refunds increased from 1000 to 3000 cents.
Net sales decreased from 9000 to 7000 cents.

Step 4: Investigate the reason for increased refunds.

Call tool: query_database
Arguments:
{
    "sql": "SELECT c.segment, SUM(r.amount_cents) AS refund_cents FROM refunds r JOIN orders o ON r.order_id = o.order_id JOIN customers c ON o.customer_id = c.customer_id WHERE r.refund_date >= '2026-09-01' AND r.refund_date < '2026-10-01' GROUP BY c.segment"
}

Step 5: Produce the final answer.

Explain the difference in net sales using actual tool results.
Include the follow-up findings only after the query has been executed.

IMPORTANT:
- Example tool results are illustrative. Always call the tools
  and use their actual returned values.
- calculate_net_sales is a TOOL, not a SQLite table.
- Never write SQL containing FROM calculate_net_sales.
- Do not write tool calls as plain text in the final answer.
- Execute tool calls through the provided tool interface.
- Do not invent the result of a follow-up SQL query.
"""

FINAL_ANSWER_INSTRUCTION = """
You have finished executing SQL queries.

Now provide the FINAL answer to the user's question
using only the SQL results already available in the conversation.

For net sales comparisons:
- Net sales = gross sales - refunds.
- Calculate net sales for each period.
- Calculate the absolute and percentage change.
- Explain whether the change was caused by gross sales,
  refunds, or both.

Do not request more SQL queries.
Do not describe what you plan to calculate.
Actually perform the calculations and present the results.

Your answer must be complete and self-contained.
"""

def get_database_schema():
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        tables = ["customers", "orders", "refunds"]
        schema = []

        for table in tables:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            columns = cursor.fetchall()

            column_descriptions = [
                f"{column[1]} {column[2]}"
                for column in columns
            ]

            schema.append(
                f"Table: {table}\n"
                + "\n".join(column_descriptions)
            )

        return "\n\n".join(schema)


class InvestigationRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    investigation_id: str | None = None
    mode: Literal["model", "offline"] = "model"


def query(sql: str, params: tuple | list = ()) -> dict:
    """Execute one bounded, read-only SQLite statement and return JSON-safe evidence."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 12000:
        return {"error": "SQL must be a nonempty string of at most 12000 characters."}
    started = time.monotonic()
    try:
        uri = DB_PATH.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=QUERY_SECONDS)) as conn:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1000000)
            conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
            conn.execute("PRAGMA query_only=ON")

            def authorizer(action, arg1, arg2, database, source):
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
                    if action == sqlite3.SQLITE_READ and (database != "main" or arg1 not in {"customers", "orders", "refunds"}):
                        return sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in ("load_extension", "readfile", "writefile"):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            conn.set_authorizer(authorizer)
            conn.set_progress_handler(lambda: int(time.monotonic() - started > QUERY_SECONDS), 1000)
            cursor = conn.execute(sql, params)
            if cursor.description is None:
                raise ValueError("Only queries returning rows are allowed")
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchmany(MAX_ROWS + 1)
            truncated = len(rows) > MAX_ROWS
            cell_truncated = False
            remaining_chars = 100000
            safe_rows = []
            for row in rows[:MAX_ROWS]:
                safe_row = []
                for value in row:
                    if isinstance(value, bytes):
                        value = "[binary data omitted]"
                        cell_truncated = True
                    elif isinstance(value, str) and len(value) > MAX_CELL_CHARS:
                        value = value[:MAX_CELL_CHARS] + "..."
                        cell_truncated = True
                    if isinstance(value, str):
                        if len(value) > remaining_chars:
                            value = value[:remaining_chars]
                            cell_truncated = True
                        remaining_chars -= len(value)
                    safe_row.append(value)
                safe_rows.append(safe_row)
            return {"columns": columns, "rows": safe_rows, "truncated": truncated, "cells_truncated": cell_truncated}
    except (sqlite3.Error, ValueError, OSError) as exc:
        return {"error": str(exc)}


#TOOL = {"type": "function", "function": {"name": "query_database", "description": "Execute a read-only SQLite query and return its columns and rows. Amounts are USD cents.", "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"], "additionalProperties": False}}}


MONTHLY_SQL = """WITH gross AS (SELECT substr(order_date, 1, 7) AS month, sum(amount_cents) AS gross_cents FROM orders GROUP BY 1), refunds_by_month AS (SELECT substr(refund_date, 1, 7) AS month, sum(amount_cents) AS refund_cents FROM refunds GROUP BY 1), months AS (SELECT month FROM gross UNION SELECT month FROM refunds_by_month) SELECT months.month, coalesce(gross.gross_cents, 0) AS gross_cents, coalesce(refunds_by_month.refund_cents, 0) AS refund_cents, coalesce(gross.gross_cents, 0) - coalesce(refunds_by_month.refund_cents, 0) AS net_cents FROM months LEFT JOIN gross ON gross.month = months.month LEFT JOIN refunds_by_month ON refunds_by_month.month = months.month ORDER BY months.month"""
SEGMENT_SQL = """WITH gross AS (SELECT substr(o.order_date,1,7) month, c.segment, sum(o.amount_cents) amount FROM orders o JOIN customers c ON c.customer_id=o.customer_id GROUP BY 1,2), refunded AS (SELECT substr(r.refund_date,1,7) month, c.segment, sum(r.amount_cents) amount FROM refunds r JOIN orders o ON o.order_id=r.order_id JOIN customers c ON c.customer_id=o.customer_id GROUP BY 1,2), keys AS (SELECT month, segment FROM gross UNION SELECT month, segment FROM refunded) SELECT keys.month, keys.segment, coalesce(gross.amount,0) gross_cents, coalesce(refunded.amount,0) refund_cents, coalesce(gross.amount,0)-coalesce(refunded.amount,0) net_cents FROM keys LEFT JOIN gross ON gross.month=keys.month AND gross.segment=keys.segment LEFT JOIN refunded ON refunded.month=keys.month AND refunded.segment=keys.segment ORDER BY keys.month, keys.segment"""


def offline_attempt(question: str) -> tuple[str, list]:
    sql = MONTHLY_SQL
    result = query(sql)
    answer = "Offline deterministic mode: this is a fixed monthly sales summary, not an answer tailored to your question. Gross and refunds are grouped independently by their respective UTC dates; net is gross minus refunds, in USD cents. Refund reasons are not available."
    if "error" in result:
        answer += " The summary query failed: " + result["error"]
    else:
        answer += " Monthly results: " + json.dumps(result["rows"]) + (" (truncated)." if result["truncated"] else ".")
    segment_sql = SEGMENT_SQL
    segment_result = query(segment_sql)
    return answer, [{"sql": sql, "result": result}, {"sql": segment_sql, "result": segment_result}]


@tool
def query_database(sql: str) -> dict:
    """Execute a read-only SQL query against the business database."""
    return query(sql)

@tool
def calculate_net_sales(start_date: str, end_date: str) -> dict:
    """
    Calculate gross sales, refunds and net sales for a date range.

    Dates use the format YYYY-MM-DD.
    The start date is inclusive and the end date is exclusive.
    Gross sales use order_date; refunds use refund_date.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    if start >= end:
        raise ValueError("start_date must be before end_date")

    gross_result = query("""
        SELECT COALESCE(SUM(amount_cents), 0) AS gross_cents
        FROM orders
        WHERE order_date >= ?
          AND order_date < ?
    """, (start_date, end_date))

    refund_result = query("""
        SELECT COALESCE(SUM(amount_cents), 0) AS refund_cents
        FROM refunds
        WHERE refund_date >= ?
          AND refund_date < ?
    """, (start_date, end_date))

    gross = gross_result["rows"][0][0]
    refunds = refund_result["rows"][0][0]

    return {
        "start_date": start_date,
        "end_date": end_date,
        "gross_cents": gross,
        "refund_cents": refunds,
        "net_cents": gross - refunds,
    }


def model_attempt(question: str, previous: list, attempts: list, responses: list):
    llm = ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        temperature=0,
    )

    llm_with_tools = llm.bind_tools([query_database])

    messages = [
        SystemMessage(
            content=SYSTEM + "\n\n" + get_database_schema()
        )
    ]

    for turn in previous:
        messages.extend([
            HumanMessage(content=turn["question"]),
            AIMessage(content=json.dumps({
                "answer": turn["answer"],
                "attempts": turn["attempts"],
            })),
        ])

    messages.append(HumanMessage(content=question))

    calls = 0

    for _ in range(10):
        model = (
            llm_with_tools
            if calls < MAX_TOOL_CALLS
            else llm
        )

        response = model.invoke(messages)
        responses.append(response.model_dump(mode="json"))

        tool_calls = response.tool_calls

        if not tool_calls:
            messages.append(response)

            if calls < 2 and calls < MAX_TOOL_CALLS:
                messages.append(HumanMessage(
                    content=(
                        "The investigation is incomplete. "
                        "Execute a follow-up SQL query based on "
                        "the previous result."
                    )
                ))
                continue

            messages.append(HumanMessage(
                content=(
                    "Now provide the FINAL answer to the original question "
                    "using the SQL results already available. "
                    "If comparing net sales, calculate net sales as gross sales "
                    "minus refunds for each period, then calculate the absolute "
                    "and percentage change. Explain what caused the change. "
                    "Do not describe what you plan to calculate. "
                    "Perform the calculations and give the final answer. "
                    "Do not request any more SQL queries."
                )
            ))

            final_response = llm.invoke(messages)
            responses.append(final_response.model_dump(mode="json"))

            return final_response.content or "No answer returned."

        if len(tool_calls) != 1:
            messages.append(HumanMessage(
                content="Request exactly one SQL query at a time."
            ))
            continue

        if calls >= MAX_TOOL_CALLS:
            return "Incomplete: SQL query limit reached."

        call = tool_calls[0]
        calls += 1

        sql = call["args"].get("sql")

        if call["name"] != "query_database":
            result = {"error": "Unknown tool"}
        elif not isinstance(sql, str):
            result = {"error": "Invalid SQL argument"}
        else:
            result = query(sql)

        attempts.append({
            "sql": sql,
            "result": result,
        })

        messages.append(response)
        messages.append(ToolMessage(
            content=json.dumps(result),
            tool_call_id=call["id"],
            name="query_database",
        ))

        if calls == 1:
            messages.append(HumanMessage(
                content=(
                    "Analyze the first query result and "
                    "choose a follow-up SQL query."
                )
            ))

    return "Incomplete investigation: iteration limit reached."


@app.post("/investigations")
def investigate(request: InvestigationRequest):
    question = request.question.strip()
    if not question:
        raise HTTPException(422, "Question must not be blank")
    with lock:
        if request.investigation_id:
            investigation = investigations.get(request.investigation_id)
            if investigation is None:
                raise HTTPException(404, "Investigation not found")
        else:
            investigation = {"id": str(uuid.uuid4()), "turns": [], "busy": False}
            investigations[investigation["id"]] = investigation
        if investigation["busy"]:
            raise HTTPException(409, "Investigation already has a request in progress")
        investigation["busy"] = True
        previous = list(investigation["turns"])

    attempts = []
    responses = []
    error = None
    model_answer = None
    try:
        if request.mode == "offline":
            answer, attempts = offline_attempt(question)
        else:
            model_answer = model_attempt(question, previous, attempts, responses)
            answer = model_answer
            if len([item for item in attempts if "error" not in item["result"]]) < 2:
                error = "Incomplete: fewer than two queries succeeded. Inspect query results and failures."
    except Exception as exc:
        answer = "Investigation could not be completed. See error and retained attempts."
        error = f"{type(exc).__name__}: {exc}"
    if request.mode == "offline" and any("error" in item["result"] for item in attempts):
        error = "One or more offline queries failed."
    turn = {"question": question, "mode": request.mode, "answer": answer, "model_answer_unverified": model_answer, "attempts": attempts, "error": error, "model_responses": responses}
    with lock:
        investigation["turns"].append(turn)
        investigation["busy"] = False
        return {"investigation_id": investigation["id"], "turn": turn}


@app.get("/investigations/{investigation_id}")
def get_investigation(investigation_id: str):
    with lock:
        investigation = investigations.get(investigation_id)
        if investigation is None:
            raise HTTPException(404, "Investigation not found")
        return {"investigation_id": investigation_id, "turns": list(investigation["turns"]), "in_progress": investigation["busy"]}


@app.get("/investigations/{investigation_id}/export")
def export_investigation(investigation_id: str):
    data = get_investigation(investigation_id)
    data["definitions"] = SYSTEM
    data["configuration"] = {"provider": "openai", "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), "query_limit": MAX_TOOL_CALLS, "row_limit": MAX_ROWS, "query_seconds": QUERY_SECONDS}
    return Response(content=json.dumps(data, indent=2, ensure_ascii=False), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="investigation-{investigation_id}.json"'})

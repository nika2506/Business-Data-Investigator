import os
import json

from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    SystemMessage,
    HumanMessage,
    AIMessage,
    ToolMessage,
)
from langchain_core.tools import tool

from .prompts import SYSTEM, FINAL_ANSWER_INSTRUCTION
from .database import get_database_schema, query
from .config import MAX_TOOL_CALLS, OPENAI_MODEL


@tool
def query_database(sql: str) -> dict:
    """Execute a read-only SQL query against the business database."""
    return query(sql)


class InvestigatorAgent:
    def __init__(self):
        # Creating ChatOpenAI eagerly would require an API key even for replay.
        self.llm = None
        self.llm_with_tools = None

    def _ensure_model(self):
        if self.llm is None:
            self.llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0)
            self.llm_with_tools = self.llm.bind_tools(
                [query_database], parallel_tool_calls=False
            )

    def offline_investigate(self, question: str) -> tuple[str, list]:
        MONTHLY_SQL = """WITH gross AS (SELECT substr(order_date, 1, 7) AS month, sum (amount_cents) AS gross_cents \
                         FROM orders \
                         GROUP BY 1), refunds_by_month AS ( \
                         SELECT substr(refund_date, 1, 7) AS month, sum (amount_cents) AS refund_cents \
                         FROM refunds \
                         GROUP BY 1), months AS ( \
                         SELECT month \
                         FROM gross \
                         UNION \
                         SELECT month \
                         FROM refunds_by_month) \
                         SELECT months.month, coalesce (gross.gross_cents, 0) AS gross_cents, coalesce (refunds_by_month.refund_cents, 0) AS refund_cents, coalesce (gross.gross_cents, 0) - coalesce (refunds_by_month.refund_cents, 0) AS net_cents \
                         FROM months LEFT JOIN gross \
                         ON gross.month = months.month LEFT JOIN refunds_by_month ON refunds_by_month.month = months.month \
                         ORDER BY months.month"""
        SEGMENT_SQL = """WITH gross AS (SELECT substr(o.order_date, 1, 7) month, c.segment, sum(o.amount_cents) amount \
                                        FROM orders o \
                                                 JOIN customers c ON c.customer_id = o.customer_id \
                                        GROUP BY 1, 2), \
                              refunded AS (SELECT substr(r.refund_date, 1, 7) month, \
                                                  c.segment, \
                                                  sum(r.amount_cents) amount \
                                           FROM refunds r \
                                                    JOIN orders o ON o.order_id = r.order_id \
                                                    JOIN customers c ON c.customer_id = o.customer_id \
                                           GROUP BY 1, 2), \
                              keys AS (SELECT month, segment \
                         FROM gross \
                         UNION \
                         SELECT month, segment \
                         FROM refunded) \
                         SELECT keys.month, keys.segment, coalesce (gross.amount, 0) gross_cents, coalesce (refunded.amount, 0) refund_cents, coalesce (gross.amount, 0)- coalesce (refunded.amount, 0) net_cents \
                         FROM keys LEFT JOIN gross \
                         ON gross.month=keys.month AND gross.segment=keys.segment LEFT JOIN refunded ON refunded.month=keys.month AND refunded.segment=keys.segment \
                         ORDER BY keys.month, keys.segment"""

        sql = MONTHLY_SQL
        result = query(sql)
        answer = "Offline deterministic mode: this is a fixed monthly sales summary, not an answer tailored to your question. Gross and refunds are grouped independently by their respective UTC dates; net is gross minus refunds, in USD cents. Refund reasons are not available."
        if "error" in result:
            answer += " The summary query failed: " + result["error"]
        else:
            answer += " Monthly results: " + json.dumps(result["rows"]) + (
                " (truncated)." if result["truncated"] else ".")
        segment_sql = SEGMENT_SQL
        segment_result = query(segment_sql)
        return answer, [{"sql": sql, "result": result}, {"sql": segment_sql, "result": segment_result}]

    def replay_investigate(self, saved_turn: dict, attempts: list) -> tuple[str, dict]:
        """Re-execute saved real LLM SQL tool calls; never invoke a model."""
        saved_responses = saved_turn.get("model_responses")
        recorded_attempts = saved_turn.get("attempts")
        if not isinstance(saved_responses, list) or not saved_responses:
            raise ValueError("No recorded model responses in this turn.")
        if not isinstance(recorded_attempts, list) or not recorded_attempts:
            raise ValueError("No recorded SQL attempts in this turn.")
        if len(recorded_attempts) > MAX_TOOL_CALLS:
            raise ValueError("Recorded investigation exceeds SQL attempt limit.")

        recorded_sql = []
        for saved in saved_responses:
            if not isinstance(saved, dict):
                raise ValueError("Invalid recorded model response.")
            response = AIMessage.model_validate(saved)
            if response.invalid_tool_calls:
                raise ValueError("Recorded response contains invalid tool calls.")
            if len(response.tool_calls) > 1:
                raise ValueError("Recorded response contains parallel tool calls.")
            for call in response.tool_calls:
                if call.get("name") != "query_database":
                    raise ValueError("Recorded response contains an unknown tool.")
                sql = call.get("args", {}).get("sql")
                if not isinstance(sql, str) or not sql.strip():
                    raise ValueError("Recorded SQL is invalid.")
                recorded_sql.append(sql)

        if not recorded_sql or len(recorded_sql) != len(recorded_attempts):
            raise ValueError("Recorded model calls and SQL attempts do not match.")
        for sql, recorded in zip(recorded_sql, recorded_attempts):
            if not isinstance(recorded, dict) or sql != recorded.get("sql"):
                raise ValueError("Saved SQL differs from recorded model tool calls.")

        mismatches = []
        for index, (sql, recorded) in enumerate(zip(recorded_sql, recorded_attempts), 1):
            actual = query(sql)  # Existing read-only SQL guard and limits.
            attempts.append({"sql": sql, "result": actual})
            expected = recorded.get("result")
            if not isinstance(expected, dict):
                mismatches.append(index)
                continue
            # Ignore incidental metadata, compare actual query evidence.
            fields = ("columns", "rows", "truncated", "error")
            if any(actual.get(key) != expected.get(key)
                   for key in fields if key in actual or key in expected):
                mismatches.append(index)

        answer = saved_turn.get("model_answer_unverified") or saved_turn.get("answer")
        if not isinstance(answer, str):
            answer = "No recorded final model answer available."
        verification = {
            "matched": not mismatches,
            "checked_queries": len(attempts),
            "mismatch_queries": mismatches,
        }
        return answer, verification

    def online_investigate(self, question: str, previous: list, attempts: list, responses: list) -> str:
        self._ensure_model()

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
        successful_queries = 0
        follow_up_attempted = False

        for _ in range(10):
            model = (
                self.llm_with_tools
                if calls < MAX_TOOL_CALLS
                else self.llm
            )

            response = model.invoke(messages)
            responses.append(response.model_dump(mode="json"))

            tool_calls = response.tool_calls

            if not tool_calls:
                messages.append(response)

                if not follow_up_attempted and calls < MAX_TOOL_CALLS:
                    if calls == 0:
                        instruction = (
                            "Execute an initial SQL query to investigate "
                            "the user's question."
                        )
                    elif successful_queries == 0:
                        instruction = (
                            "The previous SQL query failed. "
                            "Correct the SQL and try again."
                        )
                    else:
                        instruction = (
                            "Analyze the previous successful SQL result "
                            "and execute a follow-up SQL query based on it."
                        )
                    messages.append(HumanMessage(content=instruction))
                    continue

                if not follow_up_attempted:
                    return "Incomplete: no follow-up SQL query was attempted."

                messages.append(HumanMessage(
                    content=(FINAL_ANSWER_INSTRUCTION)))

                final_response = self.llm.invoke(messages)
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

            # A follow-up is an SQL attempt made after a successful result.
            if successful_queries > 0:
                follow_up_attempted = True

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

            if "error" not in result:
                successful_queries += 1

            messages.append(response)
            messages.append(ToolMessage(
                content=json.dumps(result),
                tool_call_id=call["id"],
                name="query_database",
            ))

            if successful_queries == 1 and not follow_up_attempted and calls < MAX_TOOL_CALLS:
                messages.append(HumanMessage(
                    content=(
                        "Analyze the successful SQL result and "
                        "choose a follow-up SQL query based on it."
                    )
                ))

        return "Incomplete investigation: iteration limit reached."



from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, FileResponse
from pathlib import Path
from datetime import date
from pydantic import BaseModel, Field
import uuid
import threading
import os
import copy
import json
from .schemas import InvestigationRequest
from .agent import InvestigatorAgent
from .prompts import SYSTEM
from .config import MAX_TOOL_CALLS, MAX_ROWS, QUERY_SECONDS, OPENAI_MODEL


lock = threading.Lock()
reports_lock = threading.Lock()
investigations = {}
REPORTS_FILE = Path(__file__).resolve().parent.parent / "data" / "saved_reports.json"


class SaveReportRequest(BaseModel):
    name: str = Field(min_length=1)
    investigation_id: str


class RunReportRequest(BaseModel):
    start_date: date
    end_date: date


def load_reports():
    if not REPORTS_FILE.exists():
        return []
    return json.loads(REPORTS_FILE.read_text(encoding="utf-8"))


def write_reports(reports):
    REPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = REPORTS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(REPORTS_FILE)


def has_adaptive_followup(attempts):
    # A follow-up must be attempted after a successful query.
    seen_success = False
    for attempt in attempts:
        if seen_success:
            return True
        if "error" not in attempt.get("result", {}):
            seen_success = True
    return False


app = FastAPI(title="Business Data Investigator")
agent = InvestigatorAgent()

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).parent / "static/index.html")


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
            investigation = {"id": str(uuid.uuid4()), "turns": [], "context": [], "busy": False}
            investigations[investigation["id"]] = investigation
        if investigation["busy"]:
            raise HTTPException(409, "Investigation already has a request in progress")
        investigation["busy"] = True
        previous = copy.deepcopy(investigation.get("context", [])) + copy.deepcopy(investigation["turns"])

    attempts = []
    responses = []
    error = None
    model_answer = None
    try:
        if request.mode == "offline":
            answer, attempts = agent.offline_investigate(question)
        else:
            model_answer = agent.online_investigate(question, previous, attempts, responses)
            answer = model_answer
            if not has_adaptive_followup(attempts):
                error = "Incomplete: no follow-up SQL attempt after a successful query."
    except Exception as exc:
        answer = "Investigation could not be completed. See error and retained attempts."
        error = f"{type(exc).__name__}: {exc}"
    if request.mode == "offline" and any("error" in item["result"] for item in attempts):
        error = "One or more offline queries failed."
    turn = {"question": question, "mode": request.mode, "answer": answer, "model_answer_unverified": model_answer,
            "attempts": attempts, "error": error, "model_responses": responses}
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
        return {"investigation_id": investigation_id,
                "turns": list(investigation["turns"]),
                "in_progress": investigation["busy"],
                "saved_context": copy.deepcopy(investigation.get("context", []))}


@app.get("/investigations/{investigation_id}/export")
def export_investigation(investigation_id: str):
    data = get_investigation(investigation_id)
    data["definitions"] = SYSTEM
    data["configuration"] = {"provider": "openai", "model": OPENAI_MODEL, "query_limit": MAX_TOOL_CALLS,
                             "row_limit": MAX_ROWS, "query_seconds": QUERY_SECONDS}
    return Response(content=json.dumps(data, indent=2, ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="investigation-{investigation_id}.json"'})



@app.post("/reports")
def create_report(request: SaveReportRequest):
    name = request.name.strip()
    if not name:
        raise HTTPException(422, "Report name must not be blank")
    with lock:
        investigation = investigations.get(request.investigation_id)
        if investigation is None:
            raise HTTPException(404, "Investigation not found")
        if investigation["busy"]:
            raise HTTPException(409, "Investigation is still running")
        turns = copy.deepcopy(investigation.get("context", []) + investigation["turns"])
        original_question = investigation.get("original_question") or (turns[-1]["question"] if turns else None)
    if not turns:
        raise HTTPException(422, "Investigation has no completed turns")

    report = {
        "id": str(uuid.uuid4()),
        "name": name,
        "original_question": original_question,
        "saved_turns": turns,
    }
    with reports_lock:
        reports = load_reports()
        reports.append(report)
        write_reports(reports)
    return {"id": report["id"], "name": name, "original_question": report["original_question"]}


@app.get("/reports")
def list_reports():
    with reports_lock:
        reports = load_reports()
    return [
        {"id": item["id"], "name": item["name"], "original_question": item["original_question"]}
        for item in reports
    ]


@app.post("/reports/{report_id}/run")
def run_report(report_id: str, request: RunReportRequest):
    if request.start_date >= request.end_date:
        raise HTTPException(422, "start_date must be earlier than end_date")

    with reports_lock:
        report = next((item for item in load_reports() if item["id"] == report_id), None)
    if report is None:
        raise HTTPException(404, "Report not found")

    # Keep the visible report title separate from the instructions sent to the agent.
    display_question = (
        f"{report['name']} "
        f"({request.start_date.isoformat()} – {request.end_date.isoformat()}, end exclusive)"
    )
    agent_question = (
        f"Original investigation question: {report['original_question']}\n"
        f"Investigate the same metrics for the UTC period "
        f"[{request.start_date.isoformat()}, {request.end_date.isoformat()}). "
        "This period replaces any dates mentioned in the original question. "
        "Do not compare with the original period unless the original question "
        "explicitly requests a period comparison. "
        "Execute fresh SQL queries; use only their results as numerical evidence."
    )

    # Saved SQL, results and model responses remain stored in the report, but
    # are intentionally excluded from the model context for the new run.
    previous = []
    investigation = {
        "id": str(uuid.uuid4()),
        "turns": [],
        "context": [],
        "original_question": report["original_question"],
        "source_report_id": report_id,
        "busy": True,
    }
    with lock:
        investigations[investigation["id"]] = investigation

    attempts, responses = [], []
    error = None
    try:
        answer = agent.online_investigate(agent_question, previous, attempts, responses)
        if not has_adaptive_followup(attempts):
            error = "Incomplete: no follow-up SQL attempt after a successful query."
    except Exception as exc:
        answer = "Investigation could not be completed. See error and retained attempts."
        error = f"{type(exc).__name__}: {exc}"

    turn = {
        "question": display_question,
        "mode": "model",
        "answer": answer,
        "model_answer_unverified": answer,
        "attempts": attempts,
        "error": error,
        "model_responses": responses,
    }
    with lock:
        investigation["turns"].append(turn)
        investigation["busy"] = False
    return {"investigation_id": investigation["id"], "turn": turn}

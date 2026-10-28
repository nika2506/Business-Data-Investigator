from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, FileResponse
from pathlib import Path
from pydantic import BaseModel
import uuid
import threading
import os
import json
from .schemas import InvestigationRequest
from .agent import InvestigatorAgent
from .prompts import SYSTEM
from .config import MAX_TOOL_CALLS, MAX_ROWS, QUERY_SECONDS, OPENAI_MODEL


lock = threading.Lock()
investigations = {}

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
            answer, attempts = agent.offline_investigate(question)
        else:
            model_answer = agent.online_investigate(question, previous, attempts, responses)
            answer = model_answer
            if len([item for item in attempts if "error" not in item["result"]]) < 2:
                error = "Incomplete: fewer than two queries succeeded. Inspect query results and failures."
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


class ReplayRequest(BaseModel):
    export_data: dict
    turn_index: int = -1


@app.post("/investigations/replay")
def replay_investigation(request: ReplayRequest):
    """Replay one recorded model turn from a downloaded investigation JSON."""
    turns = request.export_data.get("turns")
    if not isinstance(turns, list) or not turns:
        raise HTTPException(422, "Export must contain a nonempty turns list.")
    try:
        saved_turn = turns[request.turn_index]
    except (IndexError, TypeError):
        raise HTTPException(422, "Invalid turn index.")
    if not isinstance(saved_turn, dict) or saved_turn.get("mode") != "model":
        raise HTTPException(422, "Select a recorded model-mode turn.")

    attempts = []
    try:
        answer, verification = agent.replay_investigate(saved_turn, attempts)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc

    error = None
    if not verification["matched"]:
        error = ("Replay mismatch: SQL results differ from recorded evidence "
                 f"for queries {verification['mismatch_queries']}.")
    turn = {
        "question": saved_turn.get("question", "Recorded investigation"),
        "mode": "replay",
        "answer": answer,
        "model_answer_unverified": answer,
        "attempts": attempts,
        "error": error,
        "model_responses": saved_turn.get("model_responses", []),
        "replay_verification": verification,
    }
    investigation_id = str(uuid.uuid4())
    with lock:
        investigations[investigation_id] = {
            "id": investigation_id, "turns": [turn], "busy": False
        }
    return {"investigation_id": investigation_id, "turn": turn}


@app.get("/investigations/{investigation_id}")
def get_investigation(investigation_id: str):
    with lock:
        investigation = investigations.get(investigation_id)
        if investigation is None:
            raise HTTPException(404, "Investigation not found")
        return {"investigation_id": investigation_id,
                "turns": list(investigation["turns"]),
                "in_progress": investigation["busy"]}


@app.get("/investigations/{investigation_id}/export")
def export_investigation(investigation_id: str):
    data = get_investigation(investigation_id)
    data["definitions"] = SYSTEM
    data["configuration"] = {"provider": "openai", "model": OPENAI_MODEL, "query_limit": MAX_TOOL_CALLS,
                             "row_limit": MAX_ROWS, "query_seconds": QUERY_SECONDS}
    return Response(content=json.dumps(data, indent=2, ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="investigation-{investigation_id}.json"'})

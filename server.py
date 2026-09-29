"""FastAPI server: exposes the simulation over a small JSON API and serves the UI.

Run:  uvicorn server:app --reload     (then open http://127.0.0.1:8000)
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from exam_scheduler.models import Interval
from exam_scheduler.scenarios import SCENARIOS, build_scenario
from exam_scheduler.simulation import Simulation

FRONTEND = Path(__file__).parent / "frontend"

app = FastAPI(title="Multi-Agent Exam Scheduler", version="1.0.0")

# One in-memory simulation per browser session id.
_sessions: dict[str, Simulation] = {}
_lock = threading.Lock()


class NewSim(BaseModel):
    scenario: str = "normal"
    params: dict[str, Any] = Field(default_factory=dict)
    session: str = "default"


class StepReq(BaseModel):
    session: str = "default"
    ticks: int = Field(1, ge=1, le=200)


class EventReq(BaseModel):
    session: str = "default"
    kind: str  # "room" | "faculty"
    resource: str
    day: int
    start: int
    end: int
    reason: str = "unplanned unavailability"
    auto_step: bool = True


ALLOWED_PARAMS = {"num_exams", "num_rooms", "num_faculty", "num_days", "seed", "flakiness"}


def _get(session: str) -> Simulation:
    sim = _sessions.get(session)
    if sim is None:
        raise HTTPException(404, "no simulation for this session - create one first")
    return sim


@app.get("/api/scenarios")
def list_scenarios() -> list[dict]:
    out = []
    for name, fn in SCENARIOS.items():
        data = fn()
        out.append({"name": name, "title": data.title, "description": data.description, "configurable": name in ("stress", "random")})
    return out


@app.post("/api/sim")
def new_sim(req: NewSim) -> dict:
    params: dict[str, Any] = {}
    for k, v in req.params.items():
        if k not in ALLOWED_PARAMS or v in (None, ""):
            continue
        params[k] = float(v) if k == "flakiness" else int(v)
    if "num_exams" in params and not 1 <= params["num_exams"] <= 150:
        raise HTTPException(400, "num_exams must be between 1 and 150")
    if "num_days" in params and not 1 <= params["num_days"] <= 12:
        raise HTTPException(400, "num_days must be between 1 and 12")
    for k in ("num_rooms", "num_faculty"):
        if k in params and not 1 <= params[k] <= 40:
            raise HTTPException(400, f"{k} must be between 1 and 40")
    try:
        sim = build_scenario(req.scenario, **params)
    except KeyError as e:
        raise HTTPException(404, str(e))
    with _lock:
        _sessions[req.session] = sim
    return sim.snapshot()


@app.get("/api/sim")
def get_sim(session: str = "default") -> dict:
    return _get(session).snapshot()


@app.post("/api/sim/step")
def step(req: StepReq) -> dict:
    sim = _get(req.session)
    with _lock:
        for _ in range(req.ticks):
            sim.step()
    return sim.snapshot()


@app.post("/api/sim/run")
def run(req: StepReq) -> dict:
    sim = _get(req.session)
    with _lock:
        sim.run(max_ticks=max(req.ticks, 60))
    return sim.snapshot()


@app.post("/api/sim/event")
def inject(req: EventReq) -> dict:
    sim = _get(req.session)
    try:
        with _lock:
            sim.inject_event(req.kind, req.resource, Interval(req.day, req.start, req.end), req.reason)
            if req.auto_step:
                sim.step()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return sim.snapshot()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

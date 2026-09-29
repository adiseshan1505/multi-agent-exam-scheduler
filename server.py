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
from pydantic import BaseModel, Field, field_validator

from exam_scheduler.models import ExamSpec, Faculty, Interval, Room
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


class AddRoomReq(BaseModel):
    session: str = "default"
    id: str
    name: str = ""
    capacity: int = Field(ge=10, le=2000)
    building: str = "Main"

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("Room ID cannot be empty")
        return v


class AddFacultyReq(BaseModel):
    session: str = "default"
    id: str
    name: str
    department: str = "CS"

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("Faculty ID cannot be empty")
        return v

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Faculty name cannot be empty")
        return v.strip()


class AddExamReq(BaseModel):
    session: str = "default"
    course: str
    title: str = ""
    students: int = Field(ge=1, le=2000)
    groups: list[str] = Field(default_factory=lambda: ["GEN"])
    duration: int = Field(ge=1, le=8)
    priority: int = Field(ge=1, le=5, default=1)
    windows: list[list[int]] = Field(default_factory=list)
    preferred_day: int | None = None
    preferred_hour: int | None = None
    preferred_room: str | None = None

    @field_validator("course")
    @classmethod
    def course_not_empty(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("Course code cannot be empty")
        return v

    @field_validator("groups")
    @classmethod
    def at_least_one_group(cls, v: list[str]) -> list[str]:
        v = [g.strip() for g in v if g.strip()]
        if not v:
            raise ValueError("At least one student group is required")
        return v


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


@app.post("/api/sim/room")
def add_room(req: AddRoomReq) -> dict:
    sim = _get(req.session)
    name = req.name.strip() or req.id
    room = Room(id=req.id, name=name, capacity=req.capacity, building=req.building)
    try:
        with _lock:
            sim.inject_room(room, flakiness=sim.data.flakiness)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return sim.snapshot()


@app.post("/api/sim/faculty")
def add_faculty(req: AddFacultyReq) -> dict:
    sim = _get(req.session)
    fac = Faculty(id=req.id, name=req.name, department=req.department)
    try:
        with _lock:
            sim.inject_faculty(fac, flakiness=sim.data.flakiness)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return sim.snapshot()


@app.post("/api/sim/exam")
def add_exam(req: AddExamReq) -> dict:
    sim = _get(req.session)
    # Build windows: if user didn't provide any, default to "all days"
    num_days = sim.period.num_days
    all_days = list(range(num_days))
    if req.windows:
        # Validate day indices
        for w in req.windows:
            for d in w:
                if d < 0 or d >= num_days:
                    raise HTTPException(400, f"Day index {d} is out of range (0-{num_days - 1})")
        windows = req.windows
    else:
        windows = [all_days]
    # Validate preferred day/hour
    preferred = None
    if req.preferred_day is not None and req.preferred_hour is not None:
        if req.preferred_day < 0 or req.preferred_day >= num_days:
            raise HTTPException(400, f"Preferred day {req.preferred_day} is out of range")
        if req.preferred_hour < sim.period.day_start or req.preferred_hour + req.duration > sim.period.day_end:
            raise HTTPException(400, f"Preferred hour {req.preferred_hour} doesn't fit within the day")
        preferred = (req.preferred_day, req.preferred_hour)
    # Validate preferred room
    if req.preferred_room and req.preferred_room not in sim.rooms:
        raise HTTPException(400, f"Preferred room '{req.preferred_room}' does not exist")
    spec = ExamSpec(
        course=req.course,
        title=req.title or req.course,
        students=req.students,
        groups=frozenset(req.groups),
        duration=req.duration,
        priority=req.priority,
        windows=windows,
        preferred=preferred,
        preferred_room=req.preferred_room,
        arrival_tick=sim.tick + 1,  # arrives next tick
    )
    try:
        with _lock:
            sim.inject_exam(spec)
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

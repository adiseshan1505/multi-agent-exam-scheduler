"""Discrete-tick simulation that wires the agents together.

Each tick:
  1. scheduled disruption events fire (Resource Agents broadcast unprompted),
  2. Exam Request Agents act (submit requests, counter-offer, flag conflicts),
  3. Resource Agents process any async messages,
  4. the Coordinator processes its inbox and runs a planning round if needed.

Messages sent in tick t are read by their recipient in tick t (if the
recipient acts later in the order) or t+1, which gives a visible
request -> reply -> counter-offer rhythm in the UI.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from .agents import CoordinatorAgent, ExamRequestAgent, InvigilatorAgent, RoomAgent
from .messages import MessageBus
from .models import ExamPeriod, ExamSpec, Faculty, Interval, Room
from .validation import validate

# A selector lets a scenario pick its disruption target at fire time,
# e.g. "whoever is invigilating CS201" - so demos stay meaningful.
Selector = Callable[["Simulation"], Optional[tuple[str, Interval, str]]]


@dataclass
class ScheduledEvent:
    tick: int
    kind: str  # "room" | "faculty"
    target: Union[tuple[str, Interval, str], Selector]
    fired: bool = False
    description: str = ""

    def resolve(self, sim: "Simulation") -> Optional[tuple[str, Interval, str]]:
        return self.target(sim) if callable(self.target) else self.target


@dataclass
class ScenarioData:
    name: str
    title: str
    description: str
    period: ExamPeriod
    rooms: list[Room]
    faculty: list[Faculty]
    exams: list[ExamSpec]
    events: list[ScheduledEvent] = field(default_factory=list)
    flakiness: float = 0.0
    seed: int = 0


class Simulation:
    def __init__(self, data: ScenarioData) -> None:
        self.data = data
        self.period = data.period
        self.tick = 0
        self.events = list(data.events)
        self.fired_events: list[dict] = []
        rng = random.Random(data.seed)

        self.bus = MessageBus()
        self.coordinator = CoordinatorAgent(self.period, {r.id: r.capacity for r in data.rooms}, [f.id for f in data.faculty])
        self.rooms = {r.id: RoomAgent(r, rng=random.Random(rng.random()), flakiness=data.flakiness) for r in data.rooms}
        self.faculty = {f.id: InvigilatorAgent(f, rng=random.Random(rng.random()), flakiness=data.flakiness) for f in data.faculty}
        self.exams: dict[str, ExamRequestAgent] = {}
        for spec in data.exams:
            clashing = {o.course for o in data.exams if o.course != spec.course and o.groups & spec.groups}
            self.exams[spec.course] = ExamRequestAgent(spec, clashing, self.coordinator.id)

        for agent in [self.coordinator, *self.rooms.values(), *self.faculty.values(), *self.exams.values()]:
            self.bus.register(agent)

    def inject_exam(self, spec: ExamSpec) -> None:
        """Add a new exam to a running simulation (used by the UI / API)."""
        if spec.course in self.exams:
            raise ValueError(f"exam '{spec.course}' already exists")
        # compute clashing exams (shares student groups)
        all_specs = [a.spec for a in self.exams.values()]
        clashing = {s.course for s in all_specs if s.groups & spec.groups}
        # also update existing agents whose groups overlap with the new exam
        for course in clashing:
            self.exams[course].clashing.add(spec.course)
        agent = ExamRequestAgent(spec, clashing, self.coordinator.id)
        self.exams[spec.course] = agent
        self.bus.register(agent)
        # Update coordinator's room capacities in case rooms were added
        # (not needed here since rooms don't change, but keep spec count correct)

    def inject_room(self, room: Room, flakiness: float = 0.0) -> None:
        """Add a new room to a running simulation."""
        if room.id in self.rooms:
            raise ValueError(f"room '{room.id}' already exists")
        import random as _rand
        agent = RoomAgent(room, rng=_rand.Random(), flakiness=flakiness)
        self.rooms[room.id] = agent
        self.bus.register(agent)
        self.coordinator.rooms[room.id] = room.capacity
        self.coordinator.room_belief[room.id] = []

    def inject_faculty(self, fac: Faculty, flakiness: float = 0.0) -> None:
        """Add a new invigilator to a running simulation."""
        if fac.id in self.faculty:
            raise ValueError(f"faculty '{fac.id}' already exists")
        import random as _rand
        agent = InvigilatorAgent(fac, rng=_rand.Random(), flakiness=flakiness)
        self.faculty[fac.id] = agent
        self.bus.register(agent)
        self.coordinator.faculty.append(fac.id)
        self.coordinator.fac_belief[fac.id] = []

    # ------------------------------------------------------------------
    def inject_event(self, kind: str, resource: str, iv: Interval, reason: str) -> None:
        """Queue a disruption to fire on the next tick (used by the UI / API)."""
        if kind not in ("room", "faculty"):
            raise ValueError("kind must be 'room' or 'faculty'")
        pool = self.rooms if kind == "room" else self.faculty
        if resource not in pool:
            raise ValueError(f"unknown {kind} '{resource}'")
        if not self.period.contains(iv):
            raise ValueError("interval is outside the exam period")
        self.events.append(ScheduledEvent(self.tick + 1, kind, (resource, iv, reason), description="manual"))

    def _fire_events(self) -> None:
        for ev in self.events:
            if ev.fired or ev.tick > self.tick:
                continue
            ev.fired = True
            resolved = ev.resolve(self)
            if resolved is None:
                continue
            rid, iv, reason = resolved
            agent = self.rooms[rid] if ev.kind == "room" else self.faculty[rid]
            affected = agent.go_unavailable(iv, reason, self.period.label(iv))
            self.fired_events.append(
                {"tick": self.tick, "kind": ev.kind, "resource": rid, "interval": iv.to_dict(),
                 "label": self.period.label(iv), "reason": reason, "affected": affected}
            )

    def step(self) -> None:
        self.tick += 1
        self.bus.tick = self.tick
        self._fire_events()
        for agent in self.exams.values():
            agent.step(self.tick)
        for agent in [*self.rooms.values(), *self.faculty.values()]:
            agent.step(self.tick)
        self.coordinator.step(self.tick)

    def is_done(self) -> bool:
        return (
            not self.coordinator.pending
            and self.bus.pending_messages() == 0
            and all(e.fired for e in self.events)
            and all(a.status in ("scheduled", "hard_conflict") for a in self.exams.values())
        )

    def run(self, max_ticks: int = 100) -> int:
        """Step until quiescent (or ``max_ticks``); returns ticks executed."""
        n = 0
        while n < max_ticks and not self.is_done():
            self.step()
            n += 1
        return n

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        statuses = [a.status for a in self.exams.values()]
        return {
            "tick": self.tick,
            "done": self.is_done(),
            "exams": len(self.exams),
            "scheduled": statuses.count("scheduled"),
            "hard_conflicts": statuses.count("hard_conflict"),
            "in_progress": len(statuses) - statuses.count("scheduled") - statuses.count("hard_conflict"),
            "messages": len(self.bus.log),
            **self.coordinator.stats,
        }

    def snapshot(self, log_limit: int = 500) -> dict:
        return {
            "scenario": {"name": self.data.name, "title": self.data.title, "description": self.data.description},
            "period": self.period.to_dict(),
            "summary": self.summary(),
            "violations": validate(self),
            "coordinator": self.coordinator.snapshot(),
            "rooms": [a.snapshot() for a in self.rooms.values()],
            "faculty": [a.snapshot() for a in self.faculty.values()],
            "exams": [a.snapshot() for a in self.exams.values()],
            "events": self.fired_events,
            "upcoming_events": [
                {"tick": e.tick, "kind": e.kind, "description": e.description} for e in self.events if not e.fired
            ],
            "messages": [m.to_dict() for m in self.bus.log[-log_limit:]],
            "peas": {
                "exam": ExamRequestAgent.PEAS,
                "room": RoomAgent.PEAS,
                "faculty": InvigilatorAgent.PEAS,
                "coordinator": CoordinatorAgent.PEAS,
            },
        }

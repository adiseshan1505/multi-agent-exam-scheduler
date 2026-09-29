"""Exam Request Agent: one per course exam (course / student-group pairing)."""

from __future__ import annotations

from typing import Optional

from ..messages import Message, MsgType
from ..models import Assignment, ExamSpec, Interval
from .base import Agent


class ExamRequestAgent(Agent):
    kind = "exam"
    PEAS = {
        "Performance": "Exam gets a valid slot inside an acceptable window, as close to its preference as possible",
        "Environment": "The Coordinator's responses; its own cohort size, duration and clashing exams",
        "Actuators": "EXAM_REQUEST, COUNTER_OFFER (widen window), HARD_CONFLICT",
        "Sensors": "CONFIRM, REJECT and RESCHEDULE_NOTICE messages from the Coordinator",
    }

    def __init__(self, spec: ExamSpec, conflicts_with: set[str], coordinator_id: str = "coordinator") -> None:
        super().__init__(f"exam:{spec.course}")
        self.spec = spec
        self.conflicts_with = set(conflicts_with)  # other exams sharing students
        self.coordinator_id = coordinator_id
        self.status = "waiting"  # waiting|requested|scheduled|rejected|disrupted|hard_conflict
        self.window_index = 0
        self.assignment: Optional[Assignment] = None
        self.last_reason = ""
        self.history: list[str] = []

    @property
    def allowed_days(self) -> list[int]:
        """Cumulative window: preferred window plus every alternate offered so far."""
        days: set[int] = set()
        for w in self.spec.windows[: self.window_index + 1]:
            days.update(w)
        return sorted(days)

    def _request_payload(self) -> dict:
        s = self.spec
        return {
            "course": s.course,
            "students": s.students,
            "duration": s.duration,
            "groups": sorted(s.groups),
            "conflicts_with": sorted(self.conflicts_with),
            "days": self.allowed_days,
            "preferred": list(s.preferred) if s.preferred else None,
            "preferred_room": s.preferred_room,
            "priority": s.priority,
        }

    def step(self, tick: int) -> None:
        super().step(tick)
        if self.status == "waiting" and tick >= self.spec.arrival_tick:
            self.status = "requested"
            self.history.append(f"t{tick}: requested days {self.allowed_days}")
            self.send(
                MsgType.EXAM_REQUEST,
                self.coordinator_id,
                self._request_payload(),
                f"{self.spec.course}: {self.spec.students} students, {self.spec.duration}h, days {self.allowed_days}, prio {self.spec.priority}",
            )

    def on_message(self, msg: Message) -> None:
        p = msg.payload
        if msg.type == MsgType.CONFIRM:
            self.status = "scheduled"
            self.assignment = Assignment(
                self.spec.course, p["room"], Interval.from_dict(p["interval"]), tuple(p["invigilators"])
            )
            self.history.append(f"t{msg.tick}: confirmed {msg.summary}")

        elif msg.type == MsgType.RESCHEDULE_NOTICE:
            self.status = "disrupted"
            self.assignment = None
            self.last_reason = p.get("reason", "")
            self.history.append(f"t{msg.tick}: disrupted - {self.last_reason}")

        elif msg.type == MsgType.REJECT:
            self.assignment = None
            self.last_reason = p.get("reason", "no slot available")
            if self.window_index + 1 < len(self.spec.windows):
                # Negotiate: widen the acceptable window and try again.
                self.window_index += 1
                self.status = "requested"
                self.history.append(f"t{msg.tick}: rejected ({self.last_reason}); counter-offer days {self.allowed_days}")
                self.send(
                    MsgType.COUNTER_OFFER,
                    self.coordinator_id,
                    self._request_payload(),
                    f"{self.spec.course} counter-offers days {self.allowed_days}",
                )
            else:
                self.status = "hard_conflict"
                self.history.append(f"t{msg.tick}: no alternatives left - HARD CONFLICT")
                self.send(
                    MsgType.HARD_CONFLICT,
                    self.coordinator_id,
                    {"course": self.spec.course, "reason": self.last_reason},
                    f"{self.spec.course} HARD CONFLICT: {self.last_reason}",
                )

    def snapshot(self) -> dict:
        return {
            **super().snapshot(),
            **self.spec.to_dict(),
            "conflicts_with": sorted(self.conflicts_with),
            "status": self.status,
            "allowed_days": self.allowed_days,
            "window_index": self.window_index,
            "assignment": self.assignment.to_dict() if self.assignment else None,
            "last_reason": self.last_reason,
            "history": self.history[-8:],
        }

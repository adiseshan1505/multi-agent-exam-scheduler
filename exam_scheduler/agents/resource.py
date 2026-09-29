"""Resource Agents: one per room and one per faculty member (invigilator).

A Resource Agent is the single source of truth for its own timetable. The
Coordinator only ever sees what a Resource Agent tells it (partial
observability), and a Resource Agent can change state on its own and
broadcast that change (dynamic environment).
"""

from __future__ import annotations

import random
from typing import Optional

from ..messages import BROADCAST, Message, MsgType
from ..models import Faculty, Interval, Room
from .base import Agent


class ResourceAgent(Agent):
    kind = "resource"
    resource_kind = "resource"

    def __init__(self, agent_id: str, rng: Optional[random.Random] = None, flakiness: float = 0.0) -> None:
        super().__init__(agent_id)
        self.bookings: dict[str, Interval] = {}  # course -> interval
        self.blocked: list[tuple[Interval, str]] = []  # unavailability windows
        self.rng = rng or random.Random(0)
        # Probability that a booking is refused for reasons the Coordinator
        # could not have observed (e.g. another department grabbed the room).
        self.flakiness = flakiness
        self.stats = {"queries": 0, "accepted": 0, "rejected": 0, "cancelled": 0}

    # -- private state helpers -------------------------------------------
    def conflict_reason(self, iv: Interval, course: str) -> Optional[str]:
        for blk, reason in self.blocked:
            if blk.overlaps(iv):
                return f"unavailable ({reason})"
        for other, other_iv in self.bookings.items():
            if other != course and other_iv.overlaps(iv):
                return f"already booked by {other}"
        return None

    def extra_checks(self, payload: dict) -> Optional[str]:
        return None

    # -- synchronous protocol --------------------------------------------
    def handle_request(self, msg: Message) -> Message:
        p = msg.payload
        if msg.type == MsgType.QUERY_AVAILABILITY:
            self.stats["queries"] += 1
            return msg.reply(
                MsgType.AVAILABILITY,
                {
                    "busy": [{"course": c, **iv.to_dict()} for c, iv in self.bookings.items()],
                    "blocked": [{"reason": r, **iv.to_dict()} for iv, r in self.blocked],
                },
                f"{len(self.bookings)} booked, {len(self.blocked)} blocked window(s)",
            )

        if msg.type == MsgType.BOOK_REQUEST:
            course = p["course"]
            iv = Interval.from_dict(p["interval"])
            reason = self.conflict_reason(iv, course) or self.extra_checks(p)
            if reason is None and self.flakiness and self.rng.random() < self.flakiness:
                reason = "slot unexpectedly taken by an external booking"
                # the external event genuinely occupies the slot from now on
                self.blocked.append((iv, "external booking"))
            if reason:
                self.stats["rejected"] += 1
                return msg.reply(MsgType.BOOK_REJECT, {"course": course, "reason": reason}, f"REJECT {course}: {reason}")
            self.bookings[course] = iv
            self.stats["accepted"] += 1
            return msg.reply(MsgType.BOOK_ACCEPT, {"course": course}, f"ACCEPT {course}")

        if msg.type == MsgType.CANCEL_BOOKING:
            course = p["course"]
            if self.bookings.pop(course, None) is not None:
                self.stats["cancelled"] += 1
            return msg.reply(MsgType.CANCEL_ACK, {"course": course}, f"released {course}")

        return super().handle_request(msg)

    # -- unprompted behaviour ------------------------------------------
    def go_unavailable(self, iv: Interval, reason: str, label: str = "") -> list[str]:
        """Block a window, drop clashing bookings and broadcast the event."""
        self.blocked.append((iv, reason))
        affected = [c for c, b in self.bookings.items() if b.overlaps(iv)]
        for c in affected:
            del self.bookings[c]
        self.send(
            MsgType.UNAVAILABLE_BROADCAST,
            BROADCAST,
            {"resource": self.id, "resource_kind": self.resource_kind, "interval": iv.to_dict(), "reason": reason, "affected": affected},
            f"{self.display_name} unavailable {label or iv}: {reason}"
            + (f" -> drops {', '.join(affected)}" if affected else ""),
        )
        return affected

    @property
    def display_name(self) -> str:
        return self.id

    def snapshot(self) -> dict:
        return {
            **super().snapshot(),
            "resource_kind": self.resource_kind,
            "bookings": [{"course": c, **iv.to_dict()} for c, iv in sorted(self.bookings.items(), key=lambda x: x[1])],
            "blocked": [{"reason": r, **iv.to_dict()} for iv, r in self.blocked],
            "stats": dict(self.stats),
        }


class RoomAgent(ResourceAgent):
    resource_kind = "room"
    PEAS = {
        "Performance": "Room is never double-booked or over capacity; utilisation maximised",
        "Environment": "Its own live timetable, maintenance events, booking requests",
        "Actuators": "AVAILABILITY replies, BOOK_ACCEPT / BOOK_REJECT, UNAVAILABLE_BROADCAST",
        "Sensors": "QUERY_AVAILABILITY, BOOK_REQUEST and CANCEL_BOOKING messages; facilities alerts",
    }

    def __init__(self, room: Room, **kw) -> None:
        super().__init__(room.id, **kw)
        self.room = room

    def extra_checks(self, payload: dict) -> Optional[str]:
        if payload.get("students", 0) > self.room.capacity:
            return f"capacity {self.room.capacity} < {payload['students']} students"
        return None

    @property
    def display_name(self) -> str:
        return self.room.name

    def snapshot(self) -> dict:
        return {**super().snapshot(), **self.room.to_dict()}


class InvigilatorAgent(ResourceAgent):
    resource_kind = "faculty"
    PEAS = {
        "Performance": "Never invigilates two exams at once; standing unavailability respected",
        "Environment": "Own duty roster, sickness / leave events, booking requests",
        "Actuators": "AVAILABILITY replies, BOOK_ACCEPT / BOOK_REJECT, UNAVAILABLE_BROADCAST",
        "Sensors": "QUERY_AVAILABILITY, BOOK_REQUEST and CANCEL_BOOKING messages; HR/leave notices",
    }

    def __init__(self, faculty: Faculty, **kw) -> None:
        super().__init__(faculty.id, **kw)
        self.faculty = faculty

    @property
    def display_name(self) -> str:
        return self.faculty.name

    def snapshot(self) -> dict:
        return {**super().snapshot(), **self.faculty.to_dict()}

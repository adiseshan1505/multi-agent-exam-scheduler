"""Core domain objects: time, rooms, faculty, exam specs and assignments."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ExamPeriod:
    """The exam-period window: a list of days, each with bookable hours."""

    days: tuple[str, ...]
    day_start: int = 9  # first bookable hour (24h clock)
    day_end: int = 17  # hours are bookable up to (not including) this hour

    @property
    def num_days(self) -> int:
        return len(self.days)

    def contains(self, iv: "Interval") -> bool:
        return 0 <= iv.day < self.num_days and self.day_start <= iv.start < iv.end <= self.day_end

    def starts_for(self, duration: int) -> range:
        return range(self.day_start, self.day_end - duration + 1)

    def label(self, iv: "Interval") -> str:
        return f"{self.days[iv.day]} {iv.start:02d}:00-{iv.end:02d}:00"

    def to_dict(self) -> dict:
        return {"days": list(self.days), "day_start": self.day_start, "day_end": self.day_end}


@dataclass(frozen=True, order=True)
class Interval:
    """A half-open time block [start, end) on a given day (hours)."""

    day: int
    start: int
    end: int

    def overlaps(self, other: "Interval") -> bool:
        return self.day == other.day and self.start < other.end and other.start < self.end

    def to_dict(self) -> dict:
        return {"day": self.day, "start": self.start, "end": self.end}

    @staticmethod
    def from_dict(d: dict) -> "Interval":
        return Interval(int(d["day"]), int(d["start"]), int(d["end"]))


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    capacity: int
    building: str = "Main"

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "capacity": self.capacity, "building": self.building}


@dataclass(frozen=True)
class Faculty:
    id: str
    name: str
    department: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "department": self.department}


@dataclass
class ExamSpec:
    """What an Exam Request Agent knows about its own exam.

    ``windows`` is an ordered list of day-sets. The first entry is the
    preferred window; later entries are the alternates the agent is willing to
    counter-offer if it gets rejected.
    """

    course: str
    title: str
    students: int
    groups: frozenset[str]
    duration: int
    priority: int = 1  # higher = more important (e.g. final-year / large cohort)
    windows: list[list[int]] = field(default_factory=list)
    preferred: Optional[tuple[int, int]] = None  # soft preference (day, start hour)
    preferred_room: Optional[str] = None  # soft preference
    arrival_tick: int = 0  # when the agent submits its first request

    def to_dict(self) -> dict:
        return {
            "course": self.course,
            "title": self.title,
            "students": self.students,
            "groups": sorted(self.groups),
            "duration": self.duration,
            "priority": self.priority,
            "windows": self.windows,
            "preferred": list(self.preferred) if self.preferred else None,
            "preferred_room": self.preferred_room,
            "arrival_tick": self.arrival_tick,
        }


@dataclass(frozen=True)
class Assignment:
    course: str
    room: str
    interval: Interval
    invigilators: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "course": self.course,
            "room": self.room,
            "interval": self.interval.to_dict(),
            "invigilators": list(self.invigilators),
        }


def invigilators_needed(students: int, per_invigilator: int = 50) -> int:
    """One invigilator per ``per_invigilator`` students, minimum one."""
    return max(1, -(-students // per_invigilator))

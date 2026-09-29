"""The five demo scenarios from the case study, plus a random one.

Scenarios 1-4 use a small hand-written university so the behaviour is easy to
follow in a presentation. The stress test and the "random" scenario use the
seeded generator, so no external data is needed anywhere.
"""

from __future__ import annotations

import random
from typing import Callable, Optional

from .generator import make_period, random_exams, random_faculty, random_rooms
from .models import ExamSpec, Faculty, Interval, Room
from .simulation import ScenarioData, ScheduledEvent, Simulation

ALL = [0, 1, 2, 3, 4]


def base_rooms() -> list[Room]:
    return [
        Room("HALL-A", "Main Hall A", 200, "Main"),
        Room("HALL-B", "Main Hall B", 150, "Main"),
        Room("LH-101", "Lecture Hall 101", 80, "North"),
        Room("LH-102", "Lecture Hall 102", 80, "North"),
        Room("LH-204", "Room 204", 60, "South"),
        Room("SEM-3", "Seminar Room 3", 40, "South"),
    ]


def base_faculty(n: int = 10) -> list[Faculty]:
    people = [
        ("F01", "Prof. Iyer", "CS"), ("F02", "Dr. Mehta", "MA"), ("F03", "Dr. Khan", "EE"),
        ("F04", "Prof. Das", "PH"), ("F05", "Dr. Nair", "CS"), ("F06", "Dr. Rao", "ME"),
        ("F07", "Prof. Sen", "EE"), ("F08", "Dr. Pillai", "MA"), ("F09", "Dr. Gupta", "CS"),
        ("F10", "Prof. Bose", "HS"),
    ]
    return [Faculty(*p) for p in people[:n]]


def base_exams() -> list[ExamSpec]:
    def e(course, title, students, groups, duration, prio, windows, preferred=None, room=None, arrival=1):
        return ExamSpec(course, title, students, frozenset(groups), duration, prio, windows, preferred, room, arrival)

    return [
        e("CS101", "Programming Fundamentals", 180, {"CS-Y1"}, 3, 2, [[0, 1], [0, 1, 2], ALL], (0, 9)),
        e("MA101", "Calculus I", 190, {"CS-Y1", "EE-Y1"}, 3, 2, [[0, 1], [0, 1, 2], ALL]),
        e("PH101", "Physics I", 140, {"EE-Y1", "ME-Y1"}, 3, 2, [[1, 2], [1, 2, 3], ALL]),
        e("EE201", "Circuit Theory", 75, {"EE-Y2"}, 2, 1, [[2], [2, 3], ALL]),
        e("CS201", "Data Structures", 120, {"CS-Y2"}, 3, 2, [[1, 2], [1, 2, 3], ALL], (1, 9)),
        e("MA201", "Linear Algebra", 110, {"CS-Y2", "EE-Y2"}, 2, 1, [[2, 3], [1, 2, 3, 4], ALL]),
        e("CS301", "Operating Systems", 70, {"CS-Y3"}, 3, 3, [[3], [2, 3, 4], ALL]),
        e("CS302", "AI Foundations", 60, {"CS-Y3"}, 2, 3, [[3, 4], [2, 3, 4], ALL], None, "LH-204"),
        e("ME301", "Thermodynamics", 55, {"ME-Y3"}, 3, 1, [[2, 3], [1, 2, 3, 4], ALL]),
        e("EE401", "Power Systems", 45, {"EE-Y4"}, 2, 3, [[4], [3, 4], ALL]),
        e("CS401", "Distributed Systems", 50, {"CS-Y4"}, 2, 3, [[4], [3, 4], ALL]),
        e("HS101", "Communication Skills", 35, {"ME-Y1"}, 2, 1, [ALL]),
    ]


# ---------------------------------------------------------------------------
# selectors: choose a disruption target at fire time so the demo always hits
# something that is actually booked.
# ---------------------------------------------------------------------------
def sick_invigilator_of(course: str, reason: str) -> Callable[[Simulation], Optional[tuple[str, Interval, str]]]:
    def pick(sim: Simulation):
        a = sim.coordinator.committed.get(course)
        if not a:
            return None
        day = a.interval.day
        return a.invigilators[0], Interval(day, sim.period.day_start, sim.period.day_end), reason

    return pick


def sick_invigilator_in_tightest_slot(reason: str):
    """Hit an exam whose time slot has no spare invigilator -> swap impossible."""

    def pick(sim: Simulation):
        best = None
        for course, a in sim.coordinator.committed.items():
            spare = [
                f for f, ag in sim.faculty.items()
                if f not in a.invigilators and not any(b.overlaps(a.interval) for b in ag.bookings.values())
                and not any(b.overlaps(a.interval) for b, _ in ag.blocked)
            ]
            key = (len(spare), course)
            if best is None or key < best[0]:
                best = (key, a)
        if best is None:
            return None
        a = best[1]
        return a.invigilators[-1], a.interval, reason

    return pick


def room_slot_of(course: str, reason: str):
    """Block exactly the room window an exam is booked in (e.g. Room 204, 2 hours)."""

    def pick(sim: Simulation):
        a = sim.coordinator.committed.get(course)
        return (a.room, a.interval, reason) if a else None

    return pick


def busiest_room_day(reason: str, hours: Optional[tuple[int, int]] = None):
    def pick(sim: Simulation):
        counts: dict[tuple[str, int], int] = {}
        for a in sim.coordinator.committed.values():
            counts[(a.room, a.interval.day)] = counts.get((a.room, a.interval.day), 0) + 1
        if not counts:
            return None
        (room, day), _ = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        start, end = hours or (sim.period.day_start, sim.period.day_end)
        return room, Interval(day, start, end), reason

    return pick


# ---------------------------------------------------------------------------
def scenario_normal(**_) -> ScenarioData:
    return ScenarioData(
        name="normal",
        title="1. Normal scheduling",
        description="12 exam requests arrive together; the Coordinator queries all rooms and invigilators, "
        "runs one CSP backtracking search and books a conflict-free timetable.",
        period=make_period(5),
        rooms=base_rooms(),
        faculty=base_faculty(10),
        exams=base_exams(),
    )


def scenario_contention(**_) -> ScenarioData:
    """Two exams fight for Room 204 at the same time; a late high-priority exam bumps a low one."""
    period = make_period(3, 9, 13)  # three short mornings keep contention visible
    rooms = [Room("LH-204", "Room 204", 60, "South"), Room("HALL-A", "Main Hall A", 150, "Main")]
    faculty = base_faculty(3)

    def e(course, title, students, groups, duration, prio, windows, preferred=None, room=None, arrival=1):
        return ExamSpec(course, title, students, frozenset(groups), duration, prio, windows, preferred, room, arrival)

    exams = [
        # Both want Room 204, Monday 09:00. CS302 has higher priority.
        e("CS302", "AI Foundations", 58, {"CS-Y3"}, 2, 3, [[0], [0, 1], [0, 1, 2]], (0, 9), "LH-204"),
        e("ME301", "Thermodynamics", 55, {"ME-Y3"}, 2, 1, [[0], [0, 1], [0, 1, 2]], (0, 9), "LH-204"),
        # Three CS-Y2 exams squeezed into one short morning: at most two fit,
        # so the search has to backtrack before it can prove this.
        e("CS201", "Data Structures", 90, {"CS-Y2"}, 2, 2, [[1], [1, 2]], (1, 9)),
        e("CS202", "Discrete Maths", 80, {"CS-Y2"}, 2, 2, [[1], [1, 2]]),
        e("CS203", "Computer Organisation", 70, {"CS-Y2"}, 2, 1, [[1], [1, 2]]),
        # Arrives late with a narrow window and top priority -> may bump someone.
        e("CS401", "Distributed Systems", 100, {"CS-Y4"}, 2, 4, [[0]], (0, 9), None, 4),
    ]
    return ScenarioData(
        name="contention",
        title="2. Contention & backtracking",
        description="CS302 (priority 3) and ME301 (priority 1) both ask for Room 204 on Mon at 09:00, so priority decides who gets it. "
        "Three CS-Y2 exams all want Tuesday morning, which only has room for two. The search backtracks, "
        "rejects one, and that exam counter-offers a wider window. CS401 (priority 4) arrives late with Monday "
        "as its only option and bumps a lower-priority exam.",
        period=period,
        rooms=rooms,
        faculty=faculty,
        exams=exams,
    )


def scenario_faculty_sick(**_) -> ScenarioData:
    return ScenarioData(
        name="faculty_sick",
        title="3. Faculty suddenly unavailable",
        description="After the timetable is built, an invigilator of CS201 calls in sick for the day, and the Coordinator "
        "swaps in a free colleague without touching room or time. Later a second invigilator is lost in the busiest slot, "
        "where nobody is free to cover, so that exam is rescheduled.",
        period=make_period(5),
        rooms=base_rooms(),
        faculty=base_faculty(7),
        exams=base_exams(),
        events=[
            ScheduledEvent(4, "faculty", sick_invigilator_of("CS201", "sick leave"), description="CS201's invigilator falls sick"),
            ScheduledEvent(7, "faculty", sick_invigilator_in_tightest_slot("family emergency"), description="invigilator lost in the tightest slot"),
        ],
    )


def scenario_room_down(**_) -> ScenarioData:
    return ScenarioData(
        name="room_down",
        title="4. Room suddenly unavailable",
        description="Mid-run, the busiest room loses a full day to AC maintenance, and later CS302's room (Room 204) loses a 2-hour window for wiring repair. "
        "Only exams booked against those windows are un-assigned and re-searched. Every other booking stays fixed.",
        period=make_period(5),
        rooms=base_rooms(),
        faculty=base_faculty(10),
        exams=base_exams(),
        events=[
            ScheduledEvent(4, "room", busiest_room_day("AC maintenance"), description="busiest room: AC maintenance"),
            ScheduledEvent(6, "room", room_slot_of("CS302", "projector & wiring repair"), description="CS302's room: wiring repair"),
        ],
    )


def scenario_stress(num_exams: int = 75, num_rooms: int = 6, num_faculty: int = 12, num_days: int = 5, seed: int = 7, flakiness: float = 0.03, **_) -> ScenarioData:
    rng = random.Random(seed)
    period = make_period(num_days)
    rooms = random_rooms(rng, num_rooms)
    faculty = random_faculty(rng, num_faculty)
    exams = random_exams(rng, num_exams, period, max(r.capacity for r in rooms), arrival_spread=2)
    events = [
        ScheduledEvent(4, "room", busiest_room_day("flooding"), description="busiest room floods"),
        ScheduledEvent(5, "faculty", sick_invigilator_in_tightest_slot("sick leave"), description="invigilator sick in tightest slot"),
    ]
    return ScenarioData(
        name="stress",
        title="5. Stress test",
        description=f"A burst of {num_exams} requests against {num_rooms} rooms and {num_faculty} invigilators over {num_days} days. "
        f"Resource Agents are stochastic ({flakiness:.0%} chance to refuse a booking unexpectedly) and two disruptions hit mid-run. "
        "The system either converges or reports which exams could not be placed, and why.",
        period=period,
        rooms=rooms,
        faculty=faculty,
        exams=exams,
        events=events,
        flakiness=flakiness,
        seed=seed,
    )


def scenario_random(num_exams: int = 20, num_rooms: int = 5, num_faculty: int = 10, num_days: int = 5, seed: int = 42, flakiness: float = 0.0, **_) -> ScenarioData:
    rng = random.Random(seed)
    period = make_period(num_days)
    rooms = random_rooms(rng, num_rooms)
    exams = random_exams(rng, num_exams, period, max(r.capacity for r in rooms))
    return ScenarioData(
        name="random",
        title="Random university",
        description=f"Seed {seed}: {num_exams} generated exams, {num_rooms} rooms, {num_faculty} invigilators over {num_days} days. "
        "Use the disruption panel to break things.",
        period=period,
        rooms=rooms,
        faculty=random_faculty(rng, num_faculty),
        exams=exams,
        flakiness=flakiness,
        seed=seed,
    )


SCENARIOS: dict[str, Callable[..., ScenarioData]] = {
    "normal": scenario_normal,
    "contention": scenario_contention,
    "faculty_sick": scenario_faculty_sick,
    "room_down": scenario_room_down,
    "stress": scenario_stress,
    "random": scenario_random,
}


def build_scenario(name: str, **params) -> Simulation:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario '{name}'. Choose from: {', '.join(SCENARIOS)}")
    return Simulation(SCENARIOS[name](**params))

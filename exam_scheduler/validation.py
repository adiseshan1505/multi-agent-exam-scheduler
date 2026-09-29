"""Independent checker for the five hard constraints.

It deliberately does not trust the Coordinator's beliefs: it checks the
Coordinator's master timetable against the *true* state held by the Resource
Agents and the Exam Request Agents' own specs.
"""

from __future__ import annotations

from itertools import combinations
from typing import TYPE_CHECKING

from .models import invigilators_needed

if TYPE_CHECKING:
    from .simulation import Simulation


def validate(sim: "Simulation") -> list[str]:
    errors: list[str] = []
    committed = sim.coordinator.committed
    period = sim.period

    for course, a in committed.items():
        spec = sim.exams[course].spec
        room = sim.rooms[a.room]
        iv = a.interval
        # C1 capacity
        if room.room.capacity < spec.students:
            errors.append(f"{course}: room {a.room} capacity {room.room.capacity} < {spec.students}")
        # C5 exam-period window + duration
        if not period.contains(iv):
            errors.append(f"{course}: {iv} outside the exam period")
        if iv.end - iv.start != spec.duration:
            errors.append(f"{course}: booked {iv.end - iv.start}h but needs {spec.duration}h")
        if iv.day not in sim.exams[course].allowed_days:
            errors.append(f"{course}: day {iv.day} not in its agreed window")
        # enough invigilators
        if len(set(a.invigilators)) < invigilators_needed(spec.students):
            errors.append(f"{course}: {len(a.invigilators)} invigilator(s), needs {invigilators_needed(spec.students)}")
        # coordinator view must match what the resource agents actually hold
        if room.bookings.get(course) != iv:
            errors.append(f"{course}: {a.room} does not hold this booking")
        for f in a.invigilators:
            if sim.faculty[f].bookings.get(course) != iv:
                errors.append(f"{course}: invigilator {f} does not hold this booking")
        for blk, reason in [*room.blocked, *(b for f in a.invigilators for b in sim.faculty[f].blocked)]:
            if blk.overlaps(iv):
                errors.append(f"{course}: overlaps an unavailability window ({reason})")

    for (c1, a1), (c2, a2) in combinations(committed.items(), 2):
        if not a1.interval.overlaps(a2.interval):
            continue
        # C2 room double-booking
        if a1.room == a2.room:
            errors.append(f"room {a1.room} double-booked: {c1} & {c2}")
        # C3 invigilator double-booking
        for f in set(a1.invigilators) & set(a2.invigilators):
            errors.append(f"invigilator {f} double-booked: {c1} & {c2}")
        # C4 student-group overlap
        shared = sim.exams[c1].spec.groups & sim.exams[c2].spec.groups
        if shared:
            errors.append(f"group(s) {', '.join(sorted(shared))} sit {c1} & {c2} at the same time")

    return errors

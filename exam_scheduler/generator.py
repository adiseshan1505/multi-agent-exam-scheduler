"""Synthetic data - no external dataset is needed.

``random_scenario`` builds a plausible university (rooms, faculty, courses,
student groups) from a seed, so every run is reproducible.
"""

from __future__ import annotations

import random

from .models import ExamPeriod, ExamSpec, Faculty, Room

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
DEPTS = {"CS": "Computer Science", "EE": "Electrical Eng.", "ME": "Mechanical Eng.", "MA": "Mathematics", "PH": "Physics", "CH": "Chemistry"}
TOPICS = [
    "Foundations", "Systems", "Analysis", "Design", "Theory", "Methods", "Lab Practice",
    "Modelling", "Algorithms", "Networks", "Dynamics", "Optimisation", "Signals", "Materials",
]
SURNAMES = [
    "Iyer", "Mehta", "Khan", "Das", "Nair", "Rao", "Sen", "Pillai", "Gupta", "Bose", "Menon", "Joshi",
    "Reddy", "Kapoor", "Varma", "Shah", "Banerjee", "Kulkarni", "Chawla", "Fernandes", "Dutta", "Ghosh",
]


def make_period(num_days: int = 5, day_start: int = 9, day_end: int = 17) -> ExamPeriod:
    days = tuple(DAY_NAMES[i % len(DAY_NAMES)] + (f" W{i // len(DAY_NAMES) + 1}" if i >= len(DAY_NAMES) else "") for i in range(num_days))
    return ExamPeriod(days, day_start, day_end)


def random_rooms(rng: random.Random, n: int) -> list[Room]:
    caps = [200, 150, 120, 100, 80, 80, 60, 60, 40, 40]
    rooms = []
    for i in range(n):
        cap = caps[i] if i < len(caps) else rng.choice(caps)
        prefix = "HALL" if cap >= 120 else ("LH" if cap >= 60 else "SEM")
        rooms.append(Room(f"{prefix}-{101 + i}", f"{prefix} {101 + i}", cap, rng.choice(["North", "South", "Main"])))
    return rooms


def random_faculty(rng: random.Random, n: int) -> list[Faculty]:
    names = rng.sample(SURNAMES, min(n, len(SURNAMES)))
    while len(names) < n:
        names.append(f"{rng.choice(SURNAMES)}-{len(names)}")
    return [
        Faculty(f"F{i + 1:02d}", f"{rng.choice(['Prof.', 'Dr.'])} {name}", rng.choice(list(DEPTS)))
        for i, name in enumerate(names)
    ]


def random_exams(
    rng: random.Random,
    n: int,
    period: ExamPeriod,
    max_capacity: int,
    arrival_spread: int = 3,
) -> list[ExamSpec]:
    years = [1, 2, 3, 4]
    depts = list(DEPTS)
    exams: list[ExamSpec] = []
    used: set[str] = set()
    for _ in range(n):
        dept, year = rng.choice(depts), rng.choice(years)
        num = year * 100 + rng.randint(1, 60)
        code = f"{dept}{num}"
        while code in used:
            num += 1
            code = f"{dept}{num}"
        used.add(code)
        groups = {f"{dept}-Y{year}"}
        if rng.random() < 0.3:  # shared service course, e.g. maths taken by EE too
            groups.add(f"{rng.choice(depts)}-Y{year}")
        students = min(max_capacity, int(rng.triangular(25, 190, 60)))
        start = rng.randrange(period.num_days)
        width = rng.choice([1, 2, 2, 3])
        preferred = sorted({(start + k) % period.num_days for k in range(width)})
        wider = sorted(set(preferred) | {(start + width) % period.num_days, (start - 1) % period.num_days})
        windows = [preferred, wider, list(range(period.num_days))]
        duration = rng.choice([2, 2, 3])
        pref = None
        if rng.random() < 0.5:
            pref = (rng.choice(preferred), rng.choice(list(period.starts_for(duration))))
        exams.append(
            ExamSpec(
                course=code,
                title=f"{DEPTS[dept]} {rng.choice(TOPICS)}",
                students=students,
                groups=frozenset(groups),
                duration=duration,
                priority=3 if year == 4 else rng.choice([1, 1, 2]),
                windows=windows,
                preferred=pref,
                arrival_tick=rng.randrange(1, arrival_spread + 1),
            )
        )
    return exams

"""Constraint Satisfaction Problem model + backtracking search.

Variables : the exams that currently need a slot (the Coordinator's pending set).
Domains   : (room, interval) pairs inside each exam's allowed days.
Constraints (all hard):
  C1 room capacity >= enrolled students                         (unary)
  C2 exam lies inside the exam-period window / allowed days    (unary)
  C3 no room double-booked                                      (binary, all pairs)
  C4 no student group sits two overlapping exams                (binary, clashing pairs)
  C5 no invigilator double-booked -> enough free invigilators   (global / resource)

Search: chronological backtracking with
  * MRV ("most-constrained exam first") variable ordering,
    tie-broken by degree (# clashing unassigned exams) then priority,
  * value ordering: soft preference first, then earliest slot, then best-fit room,
  * forward checking after every assignment (prunes C3/C4/C5 violations).

Already-committed exams are *not* variables - they are fixed and only shrink
domains. That is what makes replanning after a disruption local: only the
affected exams are re-searched.

If full search fails (or exceeds its node budget) a priority-ordered fallback
places as many exams as possible, highest priority first, and returns a
diagnosis for each exam it could not place.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .models import ExamPeriod, Interval, invigilators_needed

Value = tuple[str, Interval]  # (room id, interval)


@dataclass
class ExamVar:
    course: str
    students: int
    duration: int
    days: list[int]
    conflicts_with: set[str]
    priority: int = 1
    preferred: Optional[tuple[int, int]] = None
    preferred_room: Optional[str] = None

    @property
    def invigilators(self) -> int:
        return invigilators_needed(self.students)


@dataclass
class Belief:
    """The Coordinator's (possibly stale) picture of the world."""

    rooms: dict[str, int]  # room id -> capacity
    room_busy: dict[str, list[Interval]]  # room id -> busy / blocked intervals
    faculty_busy: dict[str, list[Interval]]  # faculty id -> busy / blocked intervals
    fixed_times: dict[str, Interval]  # already-committed exam -> interval


@dataclass
class SolveResult:
    assignments: dict[str, tuple[str, Interval, tuple[str, ...]]] = field(default_factory=dict)
    unplaced: dict[str, str] = field(default_factory=dict)  # course -> reason
    nodes: int = 0
    backtracks: int = 0
    used_fallback: bool = False
    trace: list[dict] = field(default_factory=list)


class _State:
    """Mutable search state with cheap undo."""

    def __init__(self, belief: Belief) -> None:
        self.capacity = belief.rooms
        self.room_busy = {r: list(v) for r, v in belief.room_busy.items()}
        for r in self.capacity:
            self.room_busy.setdefault(r, [])
        self.fac_busy = {f: list(v) for f, v in belief.faculty_busy.items()}
        self.fac_load = {f: len(v) for f, v in self.fac_busy.items()}
        self.times = dict(belief.fixed_times)

    def room_free(self, room: str, iv: Interval) -> bool:
        return not any(b.overlaps(iv) for b in self.room_busy[room])

    def free_faculty(self, iv: Interval) -> list[str]:
        free = [f for f, busy in self.fac_busy.items() if not any(b.overlaps(iv) for b in busy)]
        # spread duty fairly: least-loaded first, then id for determinism
        free.sort(key=lambda f: (self.fac_load[f], f))
        return free

    def group_clash(self, var: ExamVar, iv: Interval) -> Optional[str]:
        for other in var.conflicts_with:
            t = self.times.get(other)
            if t is not None and t.overlaps(iv):
                return other
        return None

    def violation(self, var: ExamVar, value: Value) -> Optional[str]:
        """Return the first violated constraint for ``value``, or None."""
        room, iv = value
        if self.capacity[room] < var.students:
            return "capacity"
        if not self.room_free(room, iv):
            return "room"
        clash = self.group_clash(var, iv)
        if clash:
            return f"clash:{clash}"
        if len(self.free_faculty(iv)) < var.invigilators:
            return "invigilators"
        return None

    def assign(self, var: ExamVar, value: Value) -> tuple[str, ...]:
        room, iv = value
        invig = tuple(self.free_faculty(iv)[: var.invigilators])
        self.room_busy[room].append(iv)
        for f in invig:
            self.fac_busy[f].append(iv)
            self.fac_load[f] += 1
        self.times[var.course] = iv
        return invig

    def unassign(self, var: ExamVar, value: Value, invig: tuple[str, ...]) -> None:
        room, iv = value
        self.room_busy[room].remove(iv)
        for f in invig:
            self.fac_busy[f].remove(iv)
            self.fac_load[f] -= 1
        del self.times[var.course]


class ExamCSP:
    TRACE_LIMIT = 400

    def __init__(self, period: ExamPeriod, variables: list[ExamVar], belief: Belief, node_budget: Optional[int] = None) -> None:
        self.period = period
        self.vars = {v.course: v for v in variables}
        self.belief = belief
        self.node_budget = node_budget or (200 + 25 * len(variables))

    # -- domains ---------------------------------------------------------
    def raw_values(self, var: ExamVar) -> list[Value]:
        """All (room, interval) combinations inside the window (C2)."""
        vals = []
        for day in var.days:
            if not 0 <= day < self.period.num_days:
                continue
            for start in self.period.starts_for(var.duration):
                iv = Interval(day, start, start + var.duration)
                for room in self.belief.rooms:
                    vals.append((room, iv))
        return vals

    def order_values(self, var: ExamVar, values: list[Value]) -> list[Value]:
        def key(v: Value):
            room, iv = v
            pref_miss = 0 if var.preferred and (iv.day, iv.start) == tuple(var.preferred) else 1
            room_miss = 0 if var.preferred_room == room else 1
            waste = self.belief.rooms[room] - var.students  # best-fit keeps big halls free
            return (pref_miss, room_miss, iv.day, iv.start, waste, room)

        return sorted(values, key=key)

    def initial_domains(self, state: _State) -> dict[str, list[Value]]:
        return {
            c: self.order_values(v, [val for val in self.raw_values(v) if state.violation(v, val) is None])
            for c, v in self.vars.items()
        }

    # -- diagnosis -------------------------------------------------------
    def diagnose(self, var: ExamVar, state: _State) -> str:
        values = self.raw_values(var)
        if not values:
            return "no valid days inside the exam period"
        if max(self.belief.rooms.values(), default=0) < var.students:
            return f"no room has capacity for {var.students} students"
        counts: Counter[str] = Counter()
        clashes: set[str] = set()
        for val in values:
            why = state.violation(var, val)
            if why is None:
                continue
            if why.startswith("clash:"):
                clashes.add(why.split(":", 1)[1])
                why = "clash"
            counts[why] += 1
        # capacity-rejected values are expected (small rooms), so report the rest
        counts.pop("capacity", None)
        if not counts:
            return "lost to higher-priority exams competing for the same rooms/slots"
        parts = []
        labels = {
            "room": "big-enough rooms booked or unavailable",
            "clash": "student-group clash with " + ", ".join(sorted(clashes)),
            "invigilators": f"fewer than {var.invigilators} free invigilator(s)",
        }
        for why, n in counts.most_common():
            parts.append(f"{labels[why]} ({n} slot-options)")
        return "; ".join(parts)

    # -- search ----------------------------------------------------------
    def solve(self) -> SolveResult:
        result = SolveResult()
        if not self.vars:
            return result
        state = _State(self.belief)
        domains = self.initial_domains(state)

        # Exams with an empty domain before search can never be placed now.
        for c in [c for c, d in domains.items() if not d]:
            result.unplaced[c] = self.diagnose(self.vars[c], state)
            self._trace(result, "wipeout", c, None, "empty domain before search")
            del domains[c]

        assigned: dict[str, tuple[Value, tuple[str, ...]]] = {}
        ok = self._backtrack(state, domains, assigned, result)
        if ok:
            for c, (val, invig) in assigned.items():
                result.assignments[c] = (val[0], val[1], invig)
            return result

        # Full search failed -> priority-ordered best effort (graceful degradation)
        result.used_fallback = True
        self._trace(result, "fallback", None, None, "search failed / budget hit: priority-ordered placement")
        self._fallback(domains, result)
        return result

    def _select_var(self, domains: dict[str, list[Value]], assigned: dict) -> str:
        unassigned = [c for c in domains if c not in assigned]

        def degree(c: str) -> int:
            return sum(1 for o in self.vars[c].conflicts_with if o in domains and o not in assigned)

        return min(unassigned, key=lambda c: (len(domains[c]), -degree(c), -self.vars[c].priority, -self.vars[c].students, c))

    def _forward_check(self, state: _State, domains: dict[str, list[Value]], assigned: dict, var: ExamVar, value: Value):
        """Prune neighbours; return (saved domains, wiped-out var or None)."""
        room, iv = value
        saved: dict[str, list[Value]] = {}
        for c, dom in domains.items():
            if c in assigned:
                continue
            other = self.vars[c]
            clashes = c in var.conflicts_with or var.course in other.conflicts_with
            new = []
            for val in dom:
                r2, iv2 = val
                if iv2.overlaps(iv):
                    if r2 == room or clashes:
                        continue
                    if len(state.free_faculty(iv2)) < other.invigilators:
                        continue
                new.append(val)
            if len(new) != len(dom):
                saved[c] = dom
                domains[c] = new
                if not new:
                    return saved, c
        return saved, None

    def _backtrack(self, state: _State, domains, assigned, result: SolveResult) -> bool:
        if len(assigned) == len(domains):
            return True
        c = self._select_var(domains, assigned)
        var = self.vars[c]
        for value in domains[c]:
            if result.nodes >= self.node_budget:
                return False
            if state.violation(var, value) is not None:
                continue
            result.nodes += 1
            invig = state.assign(var, value)
            assigned[c] = (value, invig)
            self._trace(result, "assign", c, value)
            saved, wiped = self._forward_check(state, domains, assigned, var, value)
            if wiped is None and self._backtrack(state, domains, assigned, result):
                return True
            # undo and try the next value (this *is* the backtrack)
            domains.update(saved)
            del assigned[c]
            state.unassign(var, value, invig)
            if result.nodes >= self.node_budget:
                return False
            result.backtracks += 1
            detail = f"forward check wiped out {wiped}" if wiped else "dead end deeper in the search"
            self._trace(result, "backtrack", c, value, detail)
        return False

    def _fallback(self, domains: dict[str, list[Value]], result: SolveResult) -> None:
        state = _State(self.belief)
        remaining = set(domains)
        while remaining:
            live = {c: [v for v in domains[c] if state.violation(self.vars[c], v) is None] for c in remaining}
            # priority first, then most-constrained
            c = min(remaining, key=lambda c: (-self.vars[c].priority, len(live[c]), -self.vars[c].students, c))
            remaining.discard(c)
            var = self.vars[c]
            if not live[c]:
                result.unplaced[c] = self.diagnose(var, state)
                self._trace(result, "drop", c, None, result.unplaced[c])
                continue
            value = live[c][0]
            invig = state.assign(var, value)
            result.assignments[c] = (value[0], value[1], invig)
            self._trace(result, "assign", c, value, "fallback")

    def _trace(self, result: SolveResult, op: str, course: Optional[str], value: Optional[Value], detail: str = "") -> None:
        if len(result.trace) >= self.TRACE_LIMIT:
            return
        entry = {"op": op, "course": course, "detail": detail}
        if value:
            entry["room"] = value[0]
            entry["slot"] = self.period.label(value[1])
        result.trace.append(entry)

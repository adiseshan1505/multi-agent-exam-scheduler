"""Scheduler / Coordinator Agent: the single central planner."""

from __future__ import annotations

from typing import Optional

from ..csp import Belief, ExamCSP, ExamVar
from ..messages import Message, MsgType
from ..models import Assignment, ExamPeriod, Interval
from .base import Agent

# A raw belief row: (course that holds it, or None for a blocked window, interval)
BusyRow = tuple[Optional[str], Interval]


class CoordinatorAgent(Agent):
    kind = "coordinator"
    listens_to_broadcasts = True
    PEAS = {
        "Performance": "# exams placed with zero hard-constraint violations; few messages; minimal disruption on replans",
        "Environment": "Partially observable, dynamic, stochastic, multi-agent: resource state is only known via queries/broadcasts",
        "Actuators": "QUERY_AVAILABILITY, BOOK_REQUEST, CANCEL_BOOKING, CONFIRM / REJECT / RESCHEDULE_NOTICE",
        "Sensors": "EXAM_REQUEST, COUNTER_OFFER, HARD_CONFLICT, AVAILABILITY, BOOK_ACCEPT/REJECT, UNAVAILABLE_BROADCAST",
    }

    MAX_BOOKING_RETRIES = 3
    MAX_BUMPS_PER_EXAM = 2

    def __init__(self, period: ExamPeriod, rooms: dict[str, int], faculty: list[str], agent_id: str = "coordinator") -> None:
        super().__init__(agent_id)
        self.period = period
        self.rooms = dict(rooms)  # public directory: room id -> capacity
        self.faculty = list(faculty)  # public directory of invigilators

        self.requests: dict[str, ExamVar] = {}
        self.pending: list[str] = []  # courses awaiting placement (queue order)
        self.committed: dict[str, Assignment] = {}
        self.unresolved: dict[str, str] = {}  # course -> reason (hard conflicts)
        self.booking_failures: dict[str, int] = {}
        self.bump_count: dict[str, int] = {}

        # Beliefs about resources, refreshed by querying; may be stale in between.
        self.room_belief: dict[str, list[BusyRow]] = {r: [] for r in self.rooms}
        self.fac_belief: dict[str, list[BusyRow]] = {f: [] for f in self.faculty}

        self.stats = {
            "planning_rounds": 0,
            "search_nodes": 0,
            "backtracks": 0,
            "fallbacks": 0,
            "booking_rejections": 0,
            "bumps": 0,
            "disruptions": 0,
            "localized_replans": 0,
            "invigilator_swaps": 0,
        }
        self.last_trace: list[dict] = []
        self.decisions: list[dict] = []  # human-readable decision log for the UI
        self.tick = 0

    # ------------------------------------------------------------------
    def decide(self, text: str, kind: str = "info") -> None:
        self.decisions.append({"tick": self.tick, "kind": kind, "text": text})

    def label(self, iv: Interval) -> str:
        return self.period.label(iv)

    def _enqueue(self, course: str, front: bool = False) -> None:
        if course in self.pending:
            self.pending.remove(course)
        if front:
            self.pending.insert(0, course)
        else:
            self.pending.append(course)

    # ------------------------------------------------------------------
    def step(self, tick: int) -> None:
        self.tick = tick
        for msg in self.drain_inbox():
            self.on_message(msg)
        if self.pending:
            self.plan()

    def on_message(self, msg: Message) -> None:
        p = msg.payload
        if msg.type in (MsgType.EXAM_REQUEST, MsgType.COUNTER_OFFER):
            course = p["course"]
            self.requests[course] = ExamVar(
                course=course,
                students=p["students"],
                duration=p["duration"],
                days=list(p["days"]),
                conflicts_with=set(p["conflicts_with"]),
                priority=p["priority"],
                preferred=tuple(p["preferred"]) if p.get("preferred") else None,
                preferred_room=p.get("preferred_room"),
            )
            self.unresolved.pop(course, None)
            if course not in self.committed:
                self._enqueue(course)
        elif msg.type == MsgType.HARD_CONFLICT:
            course = p["course"]
            self.unresolved[course] = p.get("reason", "unknown")
            if course in self.pending:
                self.pending.remove(course)
            self.decide(f"{course} could not be placed: {self.unresolved[course]}", "error")
        elif msg.type == MsgType.UNAVAILABLE_BROADCAST:
            self.handle_disruption(p)

    # -- beliefs ---------------------------------------------------------
    def refresh_beliefs(self, which: str = "all") -> None:
        """Query Resource Agents for their current availability."""
        targets = []
        if which in ("all", "rooms"):
            targets += [(r, self.room_belief) for r in self.rooms]
        if which in ("all", "faculty"):
            targets += [(f, self.fac_belief) for f in self.faculty]
        for rid, store in targets:
            reply = self.request(MsgType.QUERY_AVAILABILITY, rid, {}, "free/busy?")
            rows: list[BusyRow] = [(b["course"], Interval.from_dict(b)) for b in reply.payload["busy"]]
            rows += [(None, Interval.from_dict(b)) for b in reply.payload["blocked"]]
            store[rid] = rows

    def build_belief(self, exclude: frozenset[str] = frozenset()) -> Belief:
        def busy(rows: list[BusyRow]) -> list[Interval]:
            return [iv for c, iv in rows if c is None or c not in exclude]

        return Belief(
            rooms=self.rooms,
            room_busy={r: busy(rows) for r, rows in self.room_belief.items()},
            faculty_busy={f: busy(rows) for f, rows in self.fac_belief.items()},
            fixed_times={c: a.interval for c, a in self.committed.items() if c not in exclude},
        )

    # -- planning --------------------------------------------------------
    def plan(self) -> None:
        self.stats["planning_rounds"] += 1
        self.refresh_beliefs()
        variables = [self.requests[c] for c in self.pending]
        csp = ExamCSP(self.period, variables, self.build_belief())
        result = csp.solve()

        self.stats["search_nodes"] += result.nodes
        self.stats["backtracks"] += result.backtracks
        self.stats["fallbacks"] += int(result.used_fallback)
        self.last_trace = result.trace
        self.decide(
            f"Planning round over {len(variables)} pending exam(s) with {len(self.committed)} fixed: "
            f"{len(result.assignments)} placed, {len(result.unplaced)} unplaced, "
            f"{result.nodes} nodes, {result.backtracks} backtrack(s)" + (" [priority fallback]" if result.used_fallback else ""),
            "plan",
        )

        # Commit in priority order so the most important bookings go first.
        order = sorted(result.assignments, key=lambda c: -self.requests[c].priority)
        for course in order:
            room, iv, invig = result.assignments[course]
            self.commit(course, room, iv, invig)

        for course in sorted(result.unplaced, key=lambda c: -self.requests[c].priority):
            reason = result.unplaced[course]
            if self.try_bump(course):
                continue
            self.reject(course, reason)

    def commit(self, course: str, room: str, iv: Interval, invig: tuple[str, ...], note: str = "") -> bool:
        """Run the booking protocol with the Resource Agents (can be refused)."""
        var = self.requests[course]
        payload = {"course": course, "interval": iv.to_dict(), "students": var.students}
        reply = self.request(MsgType.BOOK_REQUEST, room, payload, f"book {course} @ {self.label(iv)}")
        if reply.type == MsgType.BOOK_REJECT:
            return self._booking_failed(course, room, reply.payload["reason"])

        accepted: list[str] = []
        for f in invig:
            r = self.request(MsgType.BOOK_REQUEST, f, payload, f"invigilate {course} @ {self.label(iv)}")
            if r.type == MsgType.BOOK_REJECT:
                # Unwind the partial booking: release everything taken so far.
                for g in [room, *accepted]:
                    self.request(MsgType.CANCEL_BOOKING, g, {"course": course}, f"unwind {course}")
                return self._booking_failed(course, f, r.payload["reason"])
            accepted.append(f)

        self.committed[course] = Assignment(course, room, iv, tuple(invig))
        # keep beliefs in sync with what we just booked
        self.room_belief[room].append((course, iv))
        for f in invig:
            self.fac_belief[f].append((course, iv))
        if course in self.pending:
            self.pending.remove(course)
        self.booking_failures.pop(course, None)
        self.send(
            MsgType.CONFIRM,
            f"exam:{course}",
            {"room": room, "interval": iv.to_dict(), "invigilators": list(invig), "note": note},
            f"{course} -> {room} {self.label(iv)} [{', '.join(invig)}]" + (f" ({note})" if note else ""),
        )
        return True

    def _booking_failed(self, course: str, resource: str, reason: str) -> bool:
        self.stats["booking_rejections"] += 1
        n = self.booking_failures[course] = self.booking_failures.get(course, 0) + 1
        self.decide(f"{resource} refused {course} ({reason}); belief was stale - unwinding and retrying", "warn")
        if n > self.MAX_BOOKING_RETRIES:
            self.reject(course, f"bookings repeatedly refused ({reason})")
        return False

    def reject(self, course: str, reason: str) -> None:
        if course in self.pending:
            self.pending.remove(course)
        self.send(MsgType.REJECT, f"exam:{course}", {"reason": reason}, f"{course}: {reason}")

    def release(self, course: str, skip: tuple[str, ...] = ()) -> Optional[Assignment]:
        """Cancel all bookings held by ``course`` (except resources in ``skip``)."""
        a = self.committed.pop(course, None)
        if a is None:
            return None
        for rid in (a.room, *a.invigilators):
            store = self.room_belief if rid == a.room else self.fac_belief
            store[rid] = [row for row in store[rid] if row[0] != course]
            if rid not in skip:
                self.request(MsgType.CANCEL_BOOKING, rid, {"course": course}, f"release {course}")
        return a

    # -- priority-based conflict resolution -----------------------------
    def try_bump(self, course: str) -> bool:
        """Displace a lower-priority committed exam if that lets ``course`` fit."""
        var = self.requests[course]
        victims = sorted(
            (c for c in self.committed if self.requests[c].priority < var.priority and self.bump_count.get(c, 0) < self.MAX_BUMPS_PER_EXAM),
            key=lambda c: (self.requests[c].priority, self.requests[c].students, c),
        )
        for victim in victims:
            trial = ExamCSP(self.period, [var], self.build_belief(exclude=frozenset({victim})), node_budget=50).solve()
            if course not in trial.assignments:
                continue
            room, iv, invig = trial.assignments[course]
            old = self.release(victim)
            assert old is not None
            self.bump_count[victim] = self.bump_count.get(victim, 0) + 1
            self.stats["bumps"] += 1
            self.send(
                MsgType.RESCHEDULE_NOTICE,
                f"exam:{victim}",
                {"reason": f"displaced by higher-priority {course}"},
                f"{victim} displaced by {course} (priority {var.priority} > {self.requests[victim].priority})",
            )
            self.decide(
                f"Priority conflict: {course} (p{var.priority}) takes {room} {self.label(iv)}; "
                f"{victim} (p{self.requests[victim].priority}) is bumped and re-queued",
                "conflict",
            )
            self._enqueue(victim, front=True)
            if self.commit(course, room, iv, invig, note=f"bumped {victim}"):
                return True
        return False

    # -- reactive replanning --------------------------------------------
    def handle_disruption(self, p: dict) -> None:
        self.stats["disruptions"] += 1
        rid, kind, reason = p["resource"], p["resource_kind"], p["reason"]
        iv = Interval.from_dict(p["interval"])
        # Update belief immediately from the broadcast (no need to re-query).
        store = self.room_belief if kind == "room" else self.fac_belief
        store.setdefault(rid, []).append((None, iv))

        affected = [
            c
            for c, a in self.committed.items()
            if a.interval.overlaps(iv) and (a.room == rid if kind == "room" else rid in a.invigilators)
        ]
        self.decide(
            f"Broadcast: {rid} unavailable {self.label(iv)} ({reason}). "
            f"Affected: {', '.join(affected) or 'none'} ({len(affected)} of {len(self.committed)} committed exams; the rest stay fixed)",
            "disruption",
        )
        if not affected:
            return

        if kind == "faculty":
            self.refresh_beliefs("faculty")
            for course in affected:
                if not self.swap_invigilator(course, rid):
                    self._reschedule(course, rid, f"invigilator {rid} unavailable ({reason})")
        else:
            for course in affected:
                self._reschedule(course, rid, f"room {rid} unavailable ({reason})")
        self.stats["localized_replans"] += 1

    def swap_invigilator(self, course: str, lost: str) -> bool:
        """Cheapest repair: keep room & time, replace just the invigilator."""
        a = self.committed[course]
        candidates = sorted(
            (f for f in self.faculty if f not in a.invigilators and not any(b.overlaps(a.interval) for _, b in self.fac_belief[f])),
            key=lambda f: (len(self.fac_belief[f]), f),
        )
        payload = {"course": course, "interval": a.interval.to_dict(), "students": self.requests[course].students}
        for f in candidates:
            r = self.request(MsgType.BOOK_REQUEST, f, payload, f"cover {course} for {lost}")
            if r.type == MsgType.BOOK_ACCEPT:
                invig = tuple(f if x == lost else x for x in a.invigilators)
                self.committed[course] = Assignment(course, a.room, a.interval, invig)
                self.fac_belief[f].append((course, a.interval))
                self.stats["invigilator_swaps"] += 1
                self.decide(f"Repair: {f} replaces {lost} as invigilator for {course} (room & time unchanged)", "repair")
                self.send(
                    MsgType.CONFIRM,
                    f"exam:{course}",
                    {"room": a.room, "interval": a.interval.to_dict(), "invigilators": list(invig), "note": f"{f} replaces {lost}"},
                    f"{course} invigilator swap {lost} -> {f}",
                )
                return True
        return False

    def _reschedule(self, course: str, lost: str, reason: str) -> None:
        # The unavailable resource already dropped the booking itself.
        self.release(course, skip=(lost,))
        self.send(MsgType.RESCHEDULE_NOTICE, f"exam:{course}", {"reason": reason}, f"{course} must move: {reason}")
        self.decide(f"Localized replan: {course} un-assigned and re-queued ({reason})", "repair")
        self._enqueue(course, front=True)

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            **super().snapshot(),
            "pending": list(self.pending),
            "committed": {c: a.to_dict() for c, a in self.committed.items()},
            "unresolved": dict(self.unresolved),
            "stats": dict(self.stats),
            "last_trace": self.last_trace,
            "decisions": self.decisions[-200:],
        }

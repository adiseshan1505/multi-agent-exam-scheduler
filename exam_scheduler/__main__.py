"""Command-line runner: ``python -m exam_scheduler --scenario all``."""

from __future__ import annotations

import argparse

from .scenarios import SCENARIOS, build_scenario
from .simulation import Simulation
from .validation import validate

KIND_ICON = {"plan": "[plan]", "conflict": "[prio]", "disruption": "[!!]", "repair": "[fix]", "warn": "[warn]", "error": "[FAIL]", "info": "[i]"}


def print_timetable(sim: Simulation) -> None:
    rows = sorted(sim.coordinator.committed.values(), key=lambda a: (a.interval, a.room))
    print(f"  {'Slot':<22} {'Room':<8} {'Exam':<7} {'Stud':>4}  Invigilators")
    for a in rows:
        spec = sim.exams[a.course].spec
        print(f"  {sim.period.label(a.interval):<22} {a.room:<8} {a.course:<7} {spec.students:>4}  {', '.join(a.invigilators)}")


def run_one(name: str, params: dict, verbose: bool) -> bool:
    sim = build_scenario(name, **params)
    print("=" * 78)
    print(sim.data.title)
    print(sim.data.description)
    print("-" * 78)
    sim.run()
    for d in sim.coordinator.decisions:
        print(f"  t{d['tick']:<3} {KIND_ICON.get(d['kind'], '')} {d['text']}")
    if verbose:
        print("-" * 78, "\n  Message log:")
        for m in sim.bus.log:
            print(f"  t{m.tick:<3} {m.sender:>14} -> {m.recipient:<14} {m.type.value:<22} {m.summary}")
    print("-" * 78)
    print_timetable(sim)
    s = sim.summary()
    print("-" * 78)
    print(
        f"  {s['scheduled']}/{s['exams']} scheduled, {s['hard_conflicts']} hard conflict(s) | ticks {s['tick']} | "
        f"messages {s['messages']} | nodes {s['search_nodes']} | backtracks {s['backtracks']} | "
        f"bumps {s['bumps']} | swaps {s['invigilator_swaps']} | booking refusals {s['booking_rejections']}"
    )
    for course, reason in sim.coordinator.unresolved.items():
        print(f"  UNPLACED {course}: {reason}")
    errors = validate(sim)
    print("  Hard constraints:", "ALL SATISFIED" if not errors else f"{len(errors)} VIOLATION(S)")
    for e in errors:
        print("   -", e)
    return not errors


def main() -> None:
    ap = argparse.ArgumentParser(description="Multi-agent exam scheduling demo")
    ap.add_argument("--scenario", default="all", help=f"one of: all, {', '.join(SCENARIOS)}")
    ap.add_argument("--seed", type=int, help="random seed (stress / random)")
    ap.add_argument("--exams", type=int, help="number of exams (stress / random)")
    ap.add_argument("--rooms", type=int, help="number of rooms (stress / random)")
    ap.add_argument("--faculty", type=int, help="number of invigilators (stress / random)")
    ap.add_argument("--days", type=int, help="exam-period length in days (stress / random)")
    ap.add_argument("--flakiness", type=float, help="probability a resource refuses a booking (stress / random)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every message on the bus")
    args = ap.parse_args()

    params = {
        k: v
        for k, v in {
            "seed": args.seed, "num_exams": args.exams, "num_rooms": args.rooms,
            "num_faculty": args.faculty, "num_days": args.days, "flakiness": args.flakiness,
        }.items()
        if v is not None
    }
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    ok = all([run_one(n, params, args.verbose) for n in names])
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

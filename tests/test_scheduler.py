import unittest

from exam_scheduler import build_scenario
from exam_scheduler.csp import Belief, ExamCSP, ExamVar
from exam_scheduler.generator import make_period
from exam_scheduler.messages import MsgType
from exam_scheduler.models import Interval
from exam_scheduler.validation import validate


def run(name, **params):
    sim = build_scenario(name, **params)
    sim.run()
    return sim


class TestCSP(unittest.TestCase):
    def setUp(self):
        self.period = make_period(1, 9, 13)  # one 4-hour morning

    def belief(self, rooms, faculty=3):
        return Belief(rooms=rooms, room_busy={r: [] for r in rooms},
                      faculty_busy={f"F{i}": [] for i in range(faculty)}, fixed_times={})

    def test_capacity_and_room_double_booking(self):
        vs = [ExamVar(f"E{i}", 50, 2, [0], set()) for i in range(3)]
        res = ExamCSP(self.period, vs, self.belief({"R": 60, "S": 30})).solve()
        # only R fits 50 students and it only holds two 2h exams in 4 hours
        self.assertEqual(len(res.assignments), 2)
        self.assertEqual(len(res.unplaced), 1)
        ivs = [iv for _, iv, _ in res.assignments.values()]
        self.assertFalse(ivs[0].overlaps(ivs[1]))
        self.assertTrue(all(room == "R" for room, _, _ in res.assignments.values()))

    def test_student_group_clash(self):
        a = ExamVar("A", 20, 2, [0], {"B"})
        b = ExamVar("B", 20, 2, [0], {"A"})
        res = ExamCSP(self.period, [a, b], self.belief({"R1": 50, "R2": 50})).solve()
        ia, ib = res.assignments["A"][1], res.assignments["B"][1]
        self.assertFalse(ia.overlaps(ib))

    def test_backtracking_on_pigeonhole(self):
        # three clashing exams, two possible non-overlapping slots -> must backtrack
        vs = [ExamVar(c, 20, 2, [0], {"A", "B", "C"} - {c}) for c in "ABC"]
        res = ExamCSP(self.period, vs, self.belief({"R1": 50, "R2": 50})).solve()
        self.assertGreater(res.backtracks, 0)
        self.assertTrue(res.used_fallback)
        self.assertEqual(len(res.assignments), 2)

    def test_priority_wins_in_fallback(self):
        vs = [ExamVar(c, 20, 2, [0], {"A", "B", "C"} - {c}, priority=p) for c, p in [("A", 1), ("B", 3), ("C", 2)]]
        res = ExamCSP(self.period, vs, self.belief({"R1": 50})).solve()
        self.assertIn("A", res.unplaced)  # lowest priority is the one dropped

    def test_invigilator_shortage(self):
        # 120 students need 3 invigilators; only 2 exist
        res = ExamCSP(self.period, [ExamVar("BIG", 120, 2, [0], set())], self.belief({"H": 200}, faculty=2)).solve()
        self.assertIn("BIG", res.unplaced)
        self.assertIn("invigilator", res.unplaced["BIG"])

    def test_fixed_exams_are_respected(self):
        b = self.belief({"R": 50})
        b.room_busy["R"].append(Interval(0, 9, 11))
        res = ExamCSP(self.period, [ExamVar("X", 20, 2, [0], set())], b).solve()
        self.assertEqual(res.assignments["X"][1].start, 11)


class TestScenarios(unittest.TestCase):
    def test_all_scenarios_valid(self):
        for name in ["normal", "contention", "faculty_sick", "room_down", "stress", "random"]:
            with self.subTest(name=name):
                sim = run(name)
                self.assertTrue(sim.is_done())
                self.assertEqual(validate(sim), [])

    def test_normal_places_everything(self):
        sim = run("normal")
        self.assertEqual(sim.summary()["scheduled"], 12)
        self.assertEqual(sim.coordinator.stats["planning_rounds"], 1)

    def test_contention_uses_priority_backtracking_and_negotiation(self):
        sim = run("contention")
        c = sim.coordinator
        self.assertEqual(c.committed["CS302"].room, "LH-204")
        self.assertEqual(c.committed["CS302"].interval, Interval(0, 9, 11))
        self.assertGreater(c.stats["backtracks"], 0)
        self.assertGreater(c.stats["bumps"], 0)
        self.assertTrue(any(m.type == MsgType.COUNTER_OFFER for m in sim.bus.log))
        self.assertEqual(sim.summary()["scheduled"], 6)

    def test_faculty_sick_swaps_then_reschedules(self):
        sim = run("faculty_sick")
        self.assertEqual(sim.coordinator.stats["invigilator_swaps"], 1)
        self.assertTrue(any(m.type == MsgType.RESCHEDULE_NOTICE for m in sim.bus.log))
        self.assertEqual(validate(sim), [])

    def test_room_down_replans_only_affected(self):
        sim = build_scenario("room_down")
        for _ in range(3):
            sim.step()
        before = dict(sim.coordinator.committed)
        sim.run()
        hit = {c for ev in sim.fired_events for c in ev["affected"]}
        self.assertTrue(hit)
        for course, a in before.items():
            if course not in hit:
                self.assertEqual(sim.coordinator.committed[course], a, f"{course} should not move")
        self.assertEqual(validate(sim), [])

    def test_stress_reports_unplaced_with_reasons(self):
        sim = run("stress")
        s = sim.summary()
        self.assertEqual(s["scheduled"] + s["hard_conflicts"], s["exams"])
        for reason in sim.coordinator.unresolved.values():
            self.assertTrue(reason)

    def test_manual_event_injection(self):
        sim = run("normal")
        a = sim.coordinator.committed["CS101"]
        sim.inject_event("room", a.room, a.interval, "fire drill")
        sim.run()
        new = sim.coordinator.committed.get("CS101")
        self.assertTrue(new is None or (new.room, new.interval) != (a.room, a.interval))
        self.assertEqual(validate(sim), [])

    def test_bad_event_rejected(self):
        sim = build_scenario("normal")
        with self.assertRaises(ValueError):
            sim.inject_event("room", "NOPE", Interval(0, 9, 10), "x")
        with self.assertRaises(ValueError):
            sim.inject_event("room", "HALL-A", Interval(0, 8, 10), "x")


class TestRandomSeeds(unittest.TestCase):
    def test_many_seeds_never_violate_constraints(self):
        for seed in range(15):
            with self.subTest(seed=seed):
                sim = run("random", seed=seed, num_exams=30, flakiness=0.05)
                self.assertTrue(sim.is_done())
                self.assertEqual(validate(sim), [])


if __name__ == "__main__":
    unittest.main()

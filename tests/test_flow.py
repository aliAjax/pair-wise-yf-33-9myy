import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, Repository, SatelliteSchedulingService, iso, utcnow


class SatelliteFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = SatelliteSchedulingService(Path(self.tmp.name) / "test.db"); self.now = utcnow() + timedelta(hours=1)
        self.svc.create_satellite("op", "operator", {"id": "SAT1", "name": "遥感一号", "data_rate_mbps": 100, "priority": 8, "storage_capacity_mb": 100000, "tenant": "T1"})
        self.svc.create_station("op", "operator", {"id": "GS1", "name": "北京站", "weather": "clear"})
        self.svc.create_antenna("op", "operator", {"id": "ANT1", "station_id": "GS1", "max_rate_mbps": 80})
        self.window = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2)), "max_rate_mbps": 70})
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 7200})

    def tearDown(self): self.tmp.cleanup()

    def request(self, mb=35000):
        return self.svc.create_request("requester-t1", "requester", "T1", {"satellite_id": "SAT1", "data_mb": mb, "priority": 7, "deadline": iso(self.now + timedelta(days=1))})

    def test_complete_receive_and_window_change_impact(self):
        req = self.request(); schedule = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=1, minutes=30)), "rate_mbps": 60})
        self.assertEqual(schedule["status"], "scheduled")
        self.svc.transition(schedule["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(schedule["id"], "op", "operator", "", "received", {})
        req2 = self.request(10000)
        schedule2 = self.svc.schedule_request(req2["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now + timedelta(hours=1)), "ends_at": iso(self.now + timedelta(hours=1, minutes=30)), "rate_mbps": 50})
        changed = self.svc.change_window(self.window["id"], "op", "operator", {"starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=1, minutes=10))})
        impacts = {x["schedule_id"]: x for x in changed["impacts"]}
        self.assertEqual(impacts[schedule["id"]]["action"], "preserve_received_data")
        self.assertEqual(impacts[schedule2["id"]]["action"], "preempted")
        self.assertEqual(self.svc.get_schedule(schedule2["id"])["status"], "preempted")

    def test_conflicts_permissions_and_data_protection(self):
        req = self.request(10000); schedule = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=30)), "rate_mbps": 50})
        req2 = self.request(10000)
        with self.assertRaises(ApiError) as ctx:
            self.svc.schedule_request(req2["id"], "requester-t1", "requester", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=20)), "rate_mbps": 50})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.schedule_request(req2["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now + timedelta(minutes=10)), "ends_at": iso(self.now + timedelta(minutes=40)), "rate_mbps": 50})
        self.assertEqual(ctx.exception.code, "antenna_conflict")
        self.svc.transition(schedule["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(schedule["id"], "op", "operator", "", "received", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.cancel_schedule(schedule["id"], "op", "operator", "", {"reason": "测试"})
        self.assertEqual(ctx.exception.code, "received_data_protected")


    def test_storage_endpoint_and_insufficient_rejection(self):
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 100000})
        info = self.svc.storage("SAT1")
        self.assertEqual(info["storage_capacity_mb"], 100000)
        self.assertEqual(info["used_mb"], 0)
        self.assertEqual(info["available_mb"], 100000)
        window2 = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.now + timedelta(hours=3)), "ends_at": iso(self.now + timedelta(hours=5)), "max_rate_mbps": 70})
        req = self.request(60000)
        self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2)), "rate_mbps": 70})
        info = self.svc.storage("SAT1")
        self.assertEqual(info["used_mb"], 60000)
        self.assertEqual(info["available_mb"], 40000)
        req2 = self.request(50000)
        with self.assertRaises(ApiError) as ctx:
            self.svc.schedule_request(req2["id"], "op", "operator", {"window_id": window2["id"], "antenna_id": "ANT1", "starts_at": iso(self.now + timedelta(hours=3)), "ends_at": iso(self.now + timedelta(hours=5)), "rate_mbps": 70})
        self.assertEqual(ctx.exception.code, "insufficient_storage")
        self.assertEqual(ctx.exception.details["available_mb"], 40000)
        self.assertEqual(ctx.exception.details["used_mb"], 60000)

    def test_received_data_keeps_occupying_storage(self):
        req = self.request(60000)
        sched = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2)), "rate_mbps": 70})
        self.svc.transition(sched["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(sched["id"], "op", "operator", "", "received", {})
        info = self.svc.storage("SAT1")
        self.assertEqual(info["used_mb"], 60000)
        self.assertEqual(info["available_mb"], 40000)
        with self.assertRaises(ApiError) as ctx:
            self.svc.cancel_schedule(sched["id"], "op", "operator", "", {"reason": "测试"})
        self.assertEqual(ctx.exception.code, "received_data_protected")
        self.assertEqual(self.svc.storage("SAT1")["used_mb"], 60000)

    def test_window_change_releases_storage(self):
        req = self.request(60000)
        sched = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2)), "rate_mbps": 70})
        self.assertEqual(self.svc.storage("SAT1")["used_mb"], 60000)
        self.svc.change_window(self.window["id"], "op", "operator", {"starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=1))})
        self.assertEqual(self.svc.get_schedule(sched["id"])["status"], "preempted")
        self.assertEqual(self.svc.storage("SAT1")["used_mb"], 0)
        req2 = self.request(30000)
        sched2 = self.svc.schedule_request(req2["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=1)), "rate_mbps": 70})
        self.assertEqual(sched2["status"], "scheduled")
        self.assertEqual(self.svc.storage("SAT1")["used_mb"], 30000)

    def test_concurrent_schedules_do_not_overallocate(self):
        import threading
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 100000})
        window2 = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.now + timedelta(hours=3)), "ends_at": iso(self.now + timedelta(hours=5)), "max_rate_mbps": 70})
        req_a = self.request(60000)
        req_b = self.request(60000)
        results = []
        def attempt(req, window):
            svc = SatelliteSchedulingService(Path(self.tmp.name) / "test.db")
            try:
                s = svc.schedule_request(req["id"], "op", "operator", {"window_id": window["id"], "antenna_id": "ANT1", "starts_at": window["starts_at"], "ends_at": window["ends_at"], "rate_mbps": 70})
                results.append(("ok", s))
            except ApiError as exc:
                results.append(("err", exc))
        t1 = threading.Thread(target=attempt, args=(req_a, self.window))
        t2 = threading.Thread(target=attempt, args=(req_b, window2))
        t1.start(); t2.start(); t1.join(); t2.join()
        oks = [r for r in results if r[0] == "ok"]
        errs = [r for r in results if r[0] == "err"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0][1].code, "insufficient_storage")
        info = self.svc.storage("SAT1")
        self.assertEqual(info["used_mb"], 60000)
        self.assertEqual(info["available_mb"], 40000)

    def test_write_failure_rolls_back_capacity_and_schedule(self):
        req = self.request(60000)
        original = Repository.audit
        def boom(*a, **k): raise RuntimeError("write failure")
        Repository.audit = staticmethod(boom)
        try:
            with self.assertRaises(RuntimeError):
                self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2)), "rate_mbps": 70})
        finally:
            Repository.audit = original
        count = self.svc.repo.conn.execute("SELECT COUNT(*) FROM schedules WHERE request_id=?", (req["id"],)).fetchone()[0]
        self.assertEqual(count, 0)
        info = self.svc.storage("SAT1")
        self.assertEqual(info["used_mb"], 0)
        self.assertEqual(info["available_mb"], 100000)


if __name__ == "__main__": unittest.main()

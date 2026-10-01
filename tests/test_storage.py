import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, SatelliteSchedulingService, iso, utcnow


class StorageFlowTest(unittest.TestCase):
    """SAT1：100000 MB 容量，单站单窗口，用于余量、窗口变更、回滚、重排测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = SatelliteSchedulingService(Path(self.tmp.name) / "test.db")
        self.now = utcnow() + timedelta(hours=1)
        self.svc.create_satellite("op", "operator", {"id": "SAT1", "name": "遥感一号", "data_rate_mbps": 100, "priority": 8, "storage_capacity_mb": 100000, "tenant": "T1"})
        self.svc.create_station("op", "operator", {"id": "GS1", "name": "北京站", "weather": "clear"})
        self.svc.create_antenna("op", "operator", {"id": "ANT1", "station_id": "GS1", "max_rate_mbps": 80})
        self.window = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=3, minutes=20)), "max_rate_mbps": 70})

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, mb):
        return self.svc.create_request("requester-t1", "requester", "T1", {"satellite_id": "SAT1", "data_mb": mb, "priority": 7, "deadline": iso(self.now + timedelta(days=1))})

    def storage(self):
        return self.svc.storage_view("op", "operator", "", "SAT1")

    def test_storage_tracks_scheduled_receiving_received(self):
        view = self.storage()
        self.assertEqual(view["capacity_mb"], 100000.0)
        self.assertEqual(view["used_mb"], 0.0)
        self.assertEqual(view["available_mb"], 100000.0)

        req = self.request(35000)
        sched = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=90)), "rate_mbps": 60})

        view = self.storage()
        self.assertEqual(view["used_mb"], 35000.0)
        self.assertEqual(view["available_mb"], 65000.0)
        self.assertEqual(view["breakdown_mb"].get("scheduled"), 35000.0)
        self.assertTrue(any(o["schedule_id"] == sched["id"] for o in view["occupancy"]))

        self.svc.transition(sched["id"], "op", "operator", "", "receiving", {})
        view = self.storage()
        self.assertEqual(view["used_mb"], 35000.0)
        self.assertEqual(view["breakdown_mb"].get("receiving"), 35000.0)
        self.assertNotIn("scheduled", view["breakdown_mb"])

        self.svc.transition(sched["id"], "op", "operator", "", "received", {})
        view = self.storage()
        self.assertEqual(view["used_mb"], 35000.0)
        self.assertEqual(view["breakdown_mb"].get("received"), 35000.0)

    def test_window_change_releases_unreceived_keeps_received(self):
        req = self.request(35000)
        sched = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=90)), "rate_mbps": 60})
        self.svc.transition(sched["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(sched["id"], "op", "operator", "", "received", {})

        req2 = self.request(10000)
        sched2 = self.svc.schedule_request(req2["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now + timedelta(hours=1)), "ends_at": iso(self.now + timedelta(hours=1, minutes=30)), "rate_mbps": 50})
        self.assertEqual(self.storage()["used_mb"], 45000.0)

        changed = self.svc.change_window(self.window["id"], "op", "operator", {"starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=70))})
        impacts = {x["schedule_id"]: x for x in changed["impacts"]}
        self.assertEqual(impacts[sched["id"]]["action"], "preserve_received_data")
        self.assertEqual(impacts[sched["id"]]["storage_released_mb"], 0.0)
        self.assertEqual(impacts[sched2["id"]]["action"], "preempted")
        self.assertEqual(impacts[sched2["id"]]["storage_released_mb"], 10000.0)
        self.assertEqual(changed["released_mb"], 10000.0)

        view = self.storage()
        self.assertEqual(view["used_mb"], 35000.0)            # 已接收数据继续占用
        self.assertEqual(view["available_mb"], 65000.0)       # 未接收排程容量已放出
        self.assertEqual(view["breakdown_mb"], {"received": 35000.0})
        self.assertTrue(all(o["schedule_id"] != sched2["id"] for o in view["occupancy"]))

    def test_rejected_schedule_keeps_full_margin(self):
        req = self.request(60000)
        self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2)), "rate_mbps": 70})
        req2 = self.request(50000)
        with self.assertRaises(ApiError) as ctx:
            self.svc.schedule_request(req2["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=100)), "rate_mbps": 70})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "storage_capacity_exceeded")
        details = ctx.exception.details
        self.assertEqual(details["required_mb"], 50000.0)
        self.assertEqual(details["used_mb"], 60000.0)
        self.assertEqual(details["available_mb"], 40000.0)
        self.assertEqual(details["capacity_mb"], 100000.0)
        view = self.storage()
        self.assertEqual(view["used_mb"], 60000.0)            # 被拒请求没有占用任何余量
        # 被拒请求仍可在容量允许时提交更小的一批
        req3 = self.request(40000)
        self.svc.schedule_request(req3["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now + timedelta(hours=2)), "ends_at": iso(self.now + timedelta(hours=3, minutes=17)), "rate_mbps": 70})
        self.assertEqual(self.storage()["used_mb"], 100000.0)

    def test_write_failure_rolls_back_schedule_and_margin(self):
        conn = self.svc.repo.conn
        conn.execute("""CREATE TEMP TRIGGER fail_schedule_insert BEFORE INSERT ON schedules
                        BEGIN SELECT RAISE(ABORT, 'injected write failure'); END""")
        req = self.request(10000)
        try:
            with self.assertRaises(sqlite3.DatabaseError) as ctx2:
                self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=30)), "rate_mbps": 50})
            self.assertIn("injected write failure", str(ctx2.exception))
        finally:
            conn.execute("DROP TRIGGER fail_schedule_insert")

        view = self.storage()
        self.assertEqual(view["used_mb"], 0.0)                # 余量随排程一起回滚
        self.assertEqual(view["available_mb"], 100000.0)
        self.assertEqual(conn.execute("SELECT status FROM requests WHERE id=?", (req["id"],)).fetchone()["status"], "pending")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 0)
        # 回滚后同一请求可以正常排程
        sched = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=30)), "rate_mbps": 50})
        self.assertEqual(sched["status"], "scheduled")
        self.assertEqual(self.storage()["used_mb"], 10000.0)

    def test_reschedule_after_preemption_reuses_capacity(self):
        req = self.request(10000)
        sched = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now + timedelta(hours=1)), "ends_at": iso(self.now + timedelta(hours=1, minutes=30)), "rate_mbps": 50})
        self.svc.change_window(self.window["id"], "op", "operator", {"starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=1, minutes=10))})
        self.assertEqual(self.svc.get_schedule(sched["id"])["status"], "preempted")
        self.assertEqual(self.storage()["used_mb"], 0.0)      # 失效排程已放出容量

        self.svc.reschedule(req["id"], "op", "operator", {"reason": "窗口更新"})
        new_sched = self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=30)), "rate_mbps": 50})
        self.assertEqual(new_sched["status"], "scheduled")
        self.assertEqual(self.storage()["used_mb"], 10000.0)  # 旧失效行不重复占用
        rows = conn_rows(self.svc, req["id"])
        self.assertEqual(len(rows), 1)


def conn_rows(svc, request_id):
    return svc.repo.conn.execute("SELECT id,status FROM schedules WHERE request_id=?", (request_id,)).fetchall()


class SmallSatelliteStorageTest(unittest.TestCase):
    """SAT3：50000 MB 小容量星，专门验证容量不足拒绝与抢占后释放。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = SatelliteSchedulingService(Path(self.tmp.name) / "test.db")
        self.now = utcnow() + timedelta(hours=1)
        self.svc.create_satellite("op", "operator", {"id": "SAT3", "name": "小容量星", "data_rate_mbps": 200, "priority": 5, "storage_capacity_mb": 50000, "tenant": "T1"})
        self.svc.create_station("op", "operator", {"id": "GS5", "name": "西站", "weather": "clear"})
        self.svc.create_antenna("op", "operator", {"id": "ANT5", "station_id": "GS5", "max_rate_mbps": 200})
        self.window = self.svc.create_window("op", "operator", {"satellite_id": "SAT3", "station_id": "GS5", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=4)), "max_rate_mbps": 200})

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, mb):
        return self.svc.create_request("requester-t1", "requester", "T1", {"satellite_id": "SAT3", "data_mb": mb, "priority": 7, "deadline": iso(self.now + timedelta(days=1))})

    def test_reject_with_available_amount_and_release_after_preempt(self):
        r1 = self.request(30000)
        self.svc.schedule_request(r1["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT5", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=60)), "rate_mbps": 100})
        r2 = self.request(30000)
        with self.assertRaises(ApiError) as ctx:
            self.svc.schedule_request(r2["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT5", "starts_at": iso(self.now + timedelta(hours=2)), "ends_at": iso(self.now + timedelta(hours=3)), "rate_mbps": 100})
        self.assertEqual(ctx.exception.code, "storage_capacity_exceeded")
        self.assertEqual(ctx.exception.details["available_mb"], 20000.0)

        # 抢占第一批后容量完整放出，20000 的新任务可以排
        sid = self.svc.repo.conn.execute("SELECT id FROM schedules WHERE request_id=?", (r1["id"],)).fetchone()["id"]
        self.svc.emergency_preempt(sid, "cmd", "commander", {"order_id": "ORD-1", "reason": "应急任务"})
        view = self.svc.storage_view("op", "operator", "", "SAT3")
        self.assertEqual(view["available_mb"], 50000.0)
        r3 = self.request(20000)
        self.svc.schedule_request(r3["id"], "op", "operator", {"window_id": self.window["id"], "antenna_id": "ANT5", "starts_at": iso(self.now + timedelta(hours=2)), "ends_at": iso(self.now + timedelta(hours=2, minutes=30)), "rate_mbps": 100})
        self.assertEqual(self.svc.storage_view("op", "operator", "", "SAT3")["used_mb"], 20000.0)


class ConcurrentDispatchTest(unittest.TestCase):
    """两个调度员同时向同一颗星各排 60000 MB（容量 100000），只能成一个。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = SatelliteSchedulingService(Path(self.tmp.name) / "test.db")
        self.now = utcnow() + timedelta(hours=1)
        self.svc.create_satellite("op", "operator", {"id": "SATC", "name": "并发星", "data_rate_mbps": 400, "priority": 5, "storage_capacity_mb": 100000, "tenant": "T1"})
        self.svc.create_station("op", "operator", {"id": "C1", "name": "并发站甲"})
        self.svc.create_station("op", "operator", {"id": "C2", "name": "并发站乙"})
        self.svc.create_antenna("op", "operator", {"id": "CA1", "station_id": "C1", "max_rate_mbps": 300})
        self.svc.create_antenna("op", "operator", {"id": "CA2", "station_id": "C2", "max_rate_mbps": 300})
        self.w1 = self.svc.create_window("op", "operator", {"satellite_id": "SATC", "station_id": "C1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(minutes=1500)), "max_rate_mbps": 300})
        self.w2 = self.svc.create_window("op", "operator", {"satellite_id": "SATC", "station_id": "C2", "starts_at": iso(self.now + timedelta(minutes=1500)), "ends_at": iso(self.now + timedelta(minutes=3000)), "max_rate_mbps": 300})
        self.r1 = self.svc.create_request("rq", "requester", "T1", {"satellite_id": "SATC", "data_mb": 60000, "priority": 7, "deadline": iso(self.now + timedelta(days=3))})
        self.r2 = self.svc.create_request("rq", "requester", "T1", {"satellite_id": "SATC", "data_mb": 60000, "priority": 7, "deadline": iso(self.now + timedelta(days=3))})

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_operators_cannot_oversell(self):
        barrier = threading.Barrier(2)
        results = {}

        def dispatch(tag, rid, window_id, antenna, start):
            barrier.wait()
            try:
                sched = self.svc.schedule_request(rid, f"op-{tag}", "operator", {
                    "window_id": window_id, "antenna_id": antenna,
                    "starts_at": iso(start), "ends_at": iso(start + timedelta(minutes=300)), "rate_mbps": 300})
                results[tag] = ("ok", sched["id"])
            except ApiError as exc:
                results[tag] = ("rejected", exc.code, exc.details)

        t1 = threading.Thread(target=dispatch, args=("A", self.r1["id"], self.w1["id"], "CA1", self.now))
        t2 = threading.Thread(target=dispatch, args=("B", self.r2["id"], self.w2["id"], "CA2", self.now + timedelta(minutes=1500)))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(results), 2)
        statuses = {tag: value[0] for tag, value in results.items()}
        self.assertEqual(sorted(statuses.values()), ["ok", "rejected"])
        loser = next(tag for tag, value in results.items() if value[0] == "rejected")
        code, details = results[loser][1], results[loser][2]
        self.assertEqual(code, "storage_capacity_exceeded")
        self.assertEqual(details["used_mb"], 60000.0)       # 后到者看到先提交者的最新占用
        self.assertEqual(details["available_mb"], 40000.0)
        self.assertEqual(details["required_mb"], 60000.0)

        view = self.svc.storage_view("op", "operator", "", "SATC")
        self.assertEqual(view["used_mb"], 60000.0)          # 总占用绝不超过容量
        self.assertEqual(view["available_mb"], 40000.0)
        self.assertEqual(len(view["occupancy"]), 1)


if __name__ == "__main__":
    unittest.main()

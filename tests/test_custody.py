"""自动化测试：覆盖题面要求的全部场景。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from media_custody import core, db, fixtures, model  # noqa: E402


PEOPLE = [
    {"id": "P1", "name": "甲", "group": "G1"},
    {"id": "P2", "name": "乙", "group": "G1"},
    {"id": "P3", "name": "丙", "group": "G2"},
    {"id": "P4", "name": "丁", "group": "G2"},
    {"id": "P5", "name": "戊", "group": "G3"},
    {"id": "P6", "name": "己", "group": "G3"},
]


def base_batch(batch_id="B-INIT"):
    """人员 + 4 件介质 + 两箱（BX01 两件、BX02 两件）。"""
    return {
        "batch_id": batch_id,
        "events": [
            {"type": "register_people", "people": PEOPLE},
            {"type": "register_media", "media": [
                {"id": "M1", "custodian": "P1", "witness": "P3", "location": "LA"},
                {"id": "M2", "custodian": "P1", "witness": "P4", "location": "LA"},
                {"id": "M3", "custodian": "P3", "witness": "P5", "location": "LB"},
                {"id": "M4", "custodian": "P3", "witness": "P6", "location": "LB"},
            ]},
            {"type": "box_seal", "box": "BX01", "seal": "S1", "media": ["M1", "M2"],
             "custodian": "P1", "witness": "P3", "location": "LA"},
            {"type": "box_seal", "box": "BX02", "seal": "S2", "media": ["M3", "M4"],
             "custodian": "P3", "witness": "P5", "location": "LB"},
        ],
    }


def confirm(hid, side, box="BX01", generation=1, media=("M1", "M2"),
            cust="P3", wit="P5", loc="LB"):
    return {"type": "handover_confirm", "handover": hid, "side": side,
            "box": box, "generation": generation, "media": list(media),
            "new_custodian": cust, "new_witness": wit, "to_location": loc}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def apply(self, batch):
        return core.import_batch(self.conn, batch)

    def medium(self, mid):
        return self.conn.execute(
            "SELECT * FROM media WHERE medium_id=?", (mid,)).fetchone()

    def box(self, bid):
        return self.conn.execute(
            "SELECT * FROM boxes WHERE box_id=?", (bid,)).fetchone()

    def handover(self, hid):
        return self.conn.execute(
            "SELECT * FROM handovers WHERE handover_id=?", (hid,)).fetchone()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]


class TestOutOfOrderConfirmations(Case):
    """两个确认乱序到达：单方到达持有人不变，双方到齐原子改变全部介质。"""

    def test_in_before_out(self):
        self.apply(base_batch())
        # 入方确认先到
        self.apply({"batch_id": "B1", "events": [confirm("H1", "in")]})
        h = self.handover("H1")
        self.assertEqual(h["status"], "pending")
        self.assertEqual(self.box("BX01")["custodian"], "P1")  # 持有人不变
        self.assertEqual(self.medium("M1")["custodian"], "P1")
        # 出方确认后到 → 原子生效
        self.apply({"batch_id": "B2", "events": [confirm("H1", "out")]})
        h = self.handover("H1")
        self.assertEqual(h["status"], "completed")
        self.assertEqual(self.box("BX01")["custodian"], "P3")
        self.assertEqual(self.box("BX01")["location"], "LB")
        for mid in ("M1", "M2"):  # 箱内全部介质同时改变
            m = self.medium(mid)
            self.assertEqual((m["custodian"], m["witness"], m["location"]),
                             ("P3", "P5", "LB"))
        # 预约已释放
        self.assertEqual(self.count("reservations"), 0)


class TestOffByOneMediaList(Case):
    """完整清单只错一件 → 内容冲突，拒绝整批（含批内其他合法事件）。"""

    def test_conflicting_second_confirm_rejects_whole_batch(self):
        self.apply(base_batch())
        self.apply({"batch_id": "B1", "events": [confirm("H1", "out")]})
        bad = confirm("H1", "in", media=("M1", "M9"))  # 清单错一件
        with self.assertRaises(core.Reject):
            self.apply({"batch_id": "B2", "events": [
                {"type": "register_media", "media": [
                    {"id": "M9", "custodian": "P1", "witness": "P3",
                     "location": "LA"}]},
                bad,
            ]})
        # 整批回滚：M9 未登记，交接仍为 pending，持有人不变
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM media WHERE medium_id='M9'").fetchone())
        self.assertEqual(self.handover("H1")["status"], "pending")
        self.assertEqual(self.box("BX01")["custodian"], "P1")


class TestUnboxOldConfirmRace(Case):
    """开箱重封与旧代确认争用：旧代确认永远不能用于新代。"""

    def test_reseal_then_second_confirm_goes_stale(self):
        self.apply(base_batch())
        self.apply({"batch_id": "B1", "events": [confirm("H1", "out")]})
        # 开箱重封 → 第 2 代
        self.apply({"batch_id": "B2", "events": [
            {"type": "box_open", "box": "BX01"},
            {"type": "box_seal", "box": "BX01", "seal": "S1b", "media": ["M1", "M2"],
             "custodian": "P1", "witness": "P3", "location": "LA"},
        ]})
        self.assertEqual(self.box("BX01")["generation"], 2)
        # 旧代（第 1 代）的第二个确认到达 → 整笔过时，释放预约，持有人不变
        self.apply({"batch_id": "B3", "events": [confirm("H1", "in")]})
        h = self.handover("H1")
        self.assertEqual(h["status"], "stale")
        self.assertIn("代次", h["stale_reason"])
        self.assertEqual(self.box("BX01")["custodian"], "P1")
        self.assertEqual(self.count("reservations"), 0)

    def test_fresh_confirm_for_old_generation_is_stale_immediately(self):
        self.apply(base_batch())
        self.apply({"batch_id": "B1", "events": [
            {"type": "box_open", "box": "BX01"},
            {"type": "box_seal", "box": "BX01", "seal": "S1b", "media": ["M1", "M2"],
             "custodian": "P1", "witness": "P3", "location": "LA"},
        ]})
        # 全新交接 ID，但针对已不存在的第 1 代 → 直接记为过时
        self.apply({"batch_id": "B2", "events": [confirm("H9", "out", generation=1)]})
        self.assertEqual(self.handover("H9")["status"], "stale")
        self.assertEqual(self.box("BX01")["custodian"], "P1")


class TestCorrectionMidChain(Case):
    """证据链到一半发生纠错：explain 报告首次断链的事件、原因与受影响箱代。"""

    def _build(self):
        self.apply(base_batch())
        # H1 完成：BX01 移到 LB
        self.apply({"batch_id": "B1", "events": [confirm("H1", "out")]})
        self.apply({"batch_id": "B2", "events": [confirm("H1", "in")]})
        # 盘点 LB 漏报 BX01 → missing 差异
        core.record_inventory(self.conn, {
            "snapshot_id": "SN1", "location": "LB",
            "observed": [{"box": "BX02", "generation": 1, "seal": "S2"}]})
        # 双签纠错：把 BX01 的记录保管人改为 P5/P1（绕过正常交接 → 断链）
        self.apply({"batch_id": "B3", "events": [{
            "type": "correction", "correction": "C1", "snapshot": "SN1",
            "box": "BX01", "diff_kind": "missing",
            "evidence": "监控复核确认 BX01 实际由戊组接管",
            "fix": {"set": {"custodian": "P5", "witness": "P1"}},
            "signers": ["P3", "P6"]}]})
        # 纠错之后又完成一笔正常交接 H2
        self.apply({"batch_id": "B4", "events": [
            confirm("H2", "out", cust="P2", wit="P4", loc="LC")]})
        self.apply({"batch_id": "B5", "events": [
            confirm("H2", "in", cust="P2", wit="P4", loc="LC")]})

    def test_explain_reports_first_break(self):
        self._build()
        result = core.explain(self.conn, "M1")
        self.assertFalse(result["continuous"])
        brk = result["first_break"]
        self.assertEqual(brk["kind"], "correction")
        self.assertIn("C1", brk["reason"])
        self.assertEqual(brk["box"], "BX01")
        self.assertEqual(brk["generation"], 1)
        kinds = [e["kind"] for e in result["chain"]]
        self.assertEqual(kinds, ["register", "seal", "handover", "correction", "handover"])

    def test_explain_to_tx_before_correction_is_continuous(self):
        self._build()
        result = core.explain(self.conn, "M1", to_tx="B2#000")
        self.assertTrue(result["continuous"])
        self.assertIsNone(result["first_break"])
        self.assertEqual([e["kind"] for e in result["chain"]],
                         ["register", "seal", "handover"])

    def test_correction_requires_latest_snapshot_and_dual_sign(self):
        self.apply(base_batch())
        core.record_inventory(self.conn, {
            "snapshot_id": "SN1", "location": "LA", "observed": []})
        core.record_inventory(self.conn, {
            "snapshot_id": "SN2", "location": "LA", "observed": []})
        # 倒填旧盘点 → 拒绝
        with self.assertRaises(core.Reject):
            self.apply({"batch_id": "B1", "events": [{
                "type": "correction", "correction": "C1", "snapshot": "SN1",
                "box": "BX01", "diff_kind": "missing", "evidence": "x",
                "fix": {"set": {}}, "signers": ["P1", "P3"]}]})
        # 同组双签 → 拒绝
        with self.assertRaises(core.Reject):
            self.apply({"batch_id": "B2", "events": [{
                "type": "correction", "correction": "C2", "snapshot": "SN2",
                "box": "BX01", "diff_kind": "missing", "evidence": "x",
                "fix": {"set": {}}, "signers": ["P1", "P2"]}]})


class TestDuplicateAndConflictingIds(Case):
    """重复与异内容 ID：批次幂等、确认幂等、异内容拒绝。"""

    def test_duplicate_batch_is_idempotent(self):
        self.apply(base_batch())
        before = self.count("events")
        result = self.apply(base_batch())
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(self.count("events"), before)

    def test_same_batch_id_different_content_rejected(self):
        self.apply(base_batch())
        altered = base_batch()
        altered["events"][1]["media"][0]["location"] = "LZ"
        with self.assertRaises(core.Reject):
            self.apply(altered)

    def test_resend_same_confirmation_idempotent(self):
        self.apply(base_batch())
        self.apply({"batch_id": "B1", "events": [confirm("H1", "out")]})
        # 同一确认在另一个批次中补发 → 幂等，不新增交接、不改变状态
        result = self.apply({"batch_id": "B2", "events": [confirm("H1", "out")]})
        self.assertEqual(result["status"], "applied")
        h = self.handover("H1")
        self.assertEqual(h["status"], "pending")
        self.assertEqual(h["out_confirmed"], 1)
        self.assertEqual(h["in_confirmed"], 0)

    def test_same_handover_id_different_content_rejected(self):
        self.apply(base_batch())
        self.apply({"batch_id": "B1", "events": [confirm("H1", "out")]})
        with self.assertRaises(core.Reject):
            self.apply({"batch_id": "B2", "events": [
                confirm("H1", "in", loc="LC")]})


class TestInventoryDiffs(Case):
    """盘点快照差异：缺失/额外/错代三类，按箱号排序；前后快照对比。"""

    def test_three_categories_sorted(self):
        self.apply(base_batch())
        # BX02 重封到第 2 代
        self.apply({"batch_id": "B1", "events": [
            {"type": "box_open", "box": "BX02"},
            {"type": "box_seal", "box": "BX02", "seal": "S2b", "media": ["M3", "M4"],
             "custodian": "P3", "witness": "P5", "location": "LA"},
        ]})
        # 现在 LA 应有 BX01(1代) 与 BX02(2代)
        report = core.record_inventory(self.conn, {
            "snapshot_id": "SN1", "location": "LA",
            "observed": [
                {"box": "BX01", "generation": 1, "seal": "S1"},
                {"box": "BX02", "generation": 1, "seal": "S2"},   # 错代
                {"box": "BX09", "generation": 1, "seal": "S9"},   # 额外
            ]})
        self.assertEqual([d["box"] for d in report["missing"]], [])  # 无缺失
        self.assertEqual([d["box"] for d in report["extra"]], ["BX09"])
        self.assertEqual([d["box"] for d in report["wrong_generation"]], ["BX02"])
        wg = report["wrong_generation"][0]
        self.assertEqual(wg["expected"], {"generation": 2, "seal": "S2b"})
        self.assertEqual(wg["observed"], {"generation": 1, "seal": "S2"})

    def test_before_and_after_handover(self):
        self.apply(base_batch())
        before = core.record_inventory(self.conn, {
            "snapshot_id": "SN1", "location": "LA",
            "observed": [{"box": "BX01", "generation": 1, "seal": "S1"}]})
        self.assertEqual(before["missing"], [])
        self.assertEqual(before["extra"], [])
        # BX01 移交到 LB
        self.apply({"batch_id": "B1", "events": [confirm("H1", "out")]})
        self.apply({"batch_id": "B2", "events": [confirm("H1", "in")]})
        after = core.record_inventory(self.conn, {
            "snapshot_id": "SN2", "location": "LA",
            "observed": [{"box": "BX01", "generation": 1, "seal": "S1"}]})
        # 移交后：BX01 不应再出现在 LA（额外），且无缺失
        self.assertEqual([d["box"] for d in after["extra"]], ["BX01"])
        self.assertEqual(after["missing"], [])
        # 同一快照 ID 重复提交 → 幂等返回原报告
        again = core.record_inventory(self.conn, {
            "snapshot_id": "SN2", "location": "LA",
            "observed": [{"box": "BX01", "generation": 1, "seal": "S1"}]})
        self.assertTrue(again["duplicate"])

    def test_missing_category(self):
        self.apply(base_batch())
        report = core.record_inventory(self.conn, {
            "snapshot_id": "SN1", "location": "LA", "observed": []})
        self.assertEqual([d["box"] for d in report["missing"]], ["BX01"])


class TestAtomicRecovery(Case):
    """事务故障后的原子恢复：失败批次零痕迹，重启不混搭旧快照与新状态。"""

    def test_failed_batch_leaves_no_trace(self):
        self.apply(base_batch())
        snapshot_before = self._dump()
        bad = {"batch_id": "B-FAIL", "events": [
            {"type": "register_media", "media": [
                {"id": "M8", "custodian": "P1", "witness": "P3", "location": "LA"}]},
            {"type": "box_seal", "box": "BX08", "seal": "S8", "media": ["M8"],
             "custodian": "P1", "witness": "P3", "location": "LA"},
            {"type": "transfer", "medium": "M8", "new_custodian": "P9",
             "new_witness": "P5", "to_location": "LB"},  # P9 不存在 → 失败
        ]}
        with self.assertRaises(core.Reject):
            self.apply(bad)
        self.assertEqual(snapshot_before, self._dump())
        # 修正后可正常导入（失败批次未占用 batch_id）
        fixed = json.loads(json.dumps(bad))
        fixed["events"][2] = {"type": "box_open", "box": "BX08"}
        result = self.apply(fixed)
        self.assertEqual(result["status"], "applied")

    def test_crash_mid_import_via_subprocess(self):
        # 用独立进程模拟“崩溃”：进程内导入失败退出，重开库后状态须与导入前一致
        self.apply(base_batch())
        before = self._dump()
        self.conn.close()
        bad_file = os.path.join(self.tmp.name, "bad.json")
        with open(bad_file, "w", encoding="utf-8") as f:
            json.dump({"batch_id": "B-CRASH", "events": [
                {"type": "register_media", "media": [
                    {"id": "M7", "custodian": "P1", "witness": "P3",
                     "location": "LA"}]},
                {"type": "transfer", "medium": "M7", "new_custodian": "PX",
                 "new_witness": "P5", "to_location": "LB"},
            ]}, f)
        env = os.environ.copy()
        env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "media_custody", "--db", self.db_path,
             "import-batch", bad_file],
            capture_output=True, text=True, env=env, cwd=PROJECT_ROOT)
        self.assertNotEqual(proc.returncode, 0)
        # 重开库：旧状态完整，无半批痕迹
        self.conn = db.connect(self.db_path)
        self.assertEqual(before, self._dump())
        self.assertEqual(self.count("batches"), 1)

    def _dump(self):
        tables = ["batches", "events", "people", "media", "boxes", "handovers",
                  "reservations", "proofs", "snapshots", "snapshot_items",
                  "diffs", "corrections"]
        out = {}
        for t in tables:
            out[t] = [tuple(r) for r in self.conn.execute(f"SELECT * FROM {t}")]
        return out


class TestConcurrentSameBox(Case):
    """涉及同一箱的并发批次：只允许符合前序的一笔进入新归属。"""

    def test_two_pending_handovers_only_one_completes(self):
        self.apply(base_batch())
        # 两笔交接先后预约（不同新保管人）
        self.apply({"batch_id": "B1", "events": [confirm("H1", "out")]})
        self.apply({"batch_id": "B2", "events": [
            confirm("H2", "in", cust="P5", wit="P2", loc="LC")]})
        barrier = threading.Barrier(2)
        errors = []

        def worker(batch):
            try:
                barrier.wait(timeout=10)
                conn = db.connect(self.db_path)
                try:
                    core.import_batch(conn, batch)
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=(
            {"batch_id": "B3", "events": [confirm("H1", "in")]},))
        t2 = threading.Thread(target=worker, args=(
            {"batch_id": "B4", "events": [
                confirm("H2", "out", cust="P5", wit="P2", loc="LC")]},))
        t1.start(); t2.start(); t1.join(30); t2.join(30)
        self.assertEqual(errors, [])
        h1, h2 = self.handover("H1"), self.handover("H2")
        statuses = sorted([h1["status"], h2["status"]])
        self.assertEqual(statuses, ["completed", "stale"])
        # 箱归属与完成的那笔一致
        winner = h1 if h1["status"] == "completed" else h2
        self.assertEqual(self.box("BX01")["custodian"], winner["new_custodian"])
        self.assertEqual(self.count("reservations"), 0)


class TestConstraints(Case):
    def test_same_group_pair_rejected(self):
        with self.assertRaises(core.Reject):
            self.apply({"batch_id": "B0", "events": [
                {"type": "register_people", "people": PEOPLE},
                {"type": "register_media", "media": [
                    {"id": "M1", "custodian": "P1", "witness": "P2",
                     "location": "LA"}]},
            ]})

    def test_sealed_medium_cannot_transfer(self):
        self.apply(base_batch())
        with self.assertRaises(core.Reject):
            self.apply({"batch_id": "B1", "events": [
                {"type": "transfer", "medium": "M1", "new_custodian": "P5",
                 "new_witness": "P1", "to_location": "LB"}]})
        # 开箱后可以单独转移
        self.apply({"batch_id": "B2", "events": [{"type": "box_open", "box": "BX01"}]})
        self.apply({"batch_id": "B3", "events": [
            {"type": "transfer", "medium": "M1", "new_custodian": "P5",
             "new_witness": "P1", "to_location": "LB"}]})
        self.assertEqual(self.medium("M1")["custodian"], "P5")


class TestThreeBoxCrossTransferVerify(Case):
    """三箱交叉移交后，用独立小模型对比所有历史盘点。"""

    def _run_scenario(self):
        tmp_fixtures = os.path.join(self.tmp.name, "fixtures")
        fixtures.write_fixtures(tmp_fixtures)

        def load(name):
            with open(os.path.join(tmp_fixtures, name), encoding="utf-8") as f:
                return json.load(f)

        self.apply(load("batch_001_register.json"))
        self.apply(load("batch_002_boxing.json"))
        core.record_inventory(self.conn, load("snap_001_lab_a_before.json"))
        self.apply(load("batch_003_first_confirms.json"))
        # 首个确认到达后：三笔均 pending，归属未变
        for hid in ("H001", "H002", "H003"):
            self.assertEqual(self.handover(hid)["status"], "pending")
        self.assertEqual(self.box("BX01")["location"], "LAB-A")
        self.apply(load("batch_004_second_confirms.json"))
        # 交叉移交完成：BX01→LAB-B、BX02→LAB-C、BX03→LAB-A
        self.assertEqual(self.box("BX01")["location"], "LAB-B")
        self.assertEqual(self.box("BX02")["location"], "LAB-C")
        self.assertEqual(self.box("BX03")["location"], "LAB-A")
        for snap in ("snap_002_lab_a_after.json", "snap_003_lab_b_after.json",
                     "snap_004_lab_c_misread.json"):
            core.record_inventory(self.conn, load(snap))
        self.apply(load("batch_005_correction.json"))
        core.record_inventory(self.conn, load("snap_005_lab_c_recount.json"))

    def test_verify_ok(self):
        self._run_scenario()
        result = model.verify(self.conn)
        self.assertEqual(result["status"], "ok", result["mismatches"])
        self.assertEqual(result["checked"]["snapshots"], 5)
        self.assertEqual(result["checked"]["boxes"], 3)
        self.assertEqual(result["checked"]["media"], 6)

    def test_snap004_has_wrong_generation_diff(self):
        self._run_scenario()
        rows = self.conn.execute(
            "SELECT * FROM diffs WHERE snapshot_id='SNAP004'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "wrong_generation")
        self.assertEqual(rows[0]["box"], "BX02")

    def test_verify_detects_tampered_diff(self):
        self._run_scenario()
        self.conn.execute(
            "UPDATE diffs SET box='BX09' WHERE snapshot_id='SNAP004'")
        result = model.verify(self.conn)
        self.assertEqual(result["status"], "mismatch")
        self.assertTrue(any("SNAP004" in m for m in result["mismatches"]))

    def test_verify_detects_tampered_state(self):
        self._run_scenario()
        self.conn.execute(
            "UPDATE boxes SET custodian='P06' WHERE box_id='BX01'")
        result = model.verify(self.conn)
        self.assertEqual(result["status"], "mismatch")

    def test_explain_after_cross_transfer(self):
        self._run_scenario()
        result = core.explain(self.conn, "M001")
        self.assertTrue(result["continuous"])
        kinds = [e["kind"] for e in result["chain"]]
        self.assertEqual(kinds, ["register", "seal", "handover"])
        self.assertEqual(result["chain"][-1]["location"], "LAB-B")


class TestFixturesReproducible(unittest.TestCase):
    def test_byte_identical_regeneration(self):
        with tempfile.TemporaryDirectory() as d1, \
                tempfile.TemporaryDirectory() as d2:
            fixtures.write_fixtures(d1)
            fixtures.write_fixtures(d2)
            names = sorted(os.listdir(d1))
            self.assertEqual(names, sorted(os.listdir(d2)))
            for name in names:
                with open(os.path.join(d1, name), "rb") as f:
                    b1 = f.read()
                with open(os.path.join(d2, name), "rb") as f:
                    b2 = f.read()
                self.assertEqual(b1, b2, name)


if __name__ == "__main__":
    unittest.main()

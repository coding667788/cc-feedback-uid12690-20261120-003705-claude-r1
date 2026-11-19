"""核心状态机：批次导入、事件应用、盘点、证据链解释。

事件类型：
  register_people   登记人员 {people:[{id,name,group}]}
  register_media    登记介质 {media:[{id,custodian,witness,location}]}
  box_seal          装箱/重封 {box,seal,media:[...],custodian,witness,location}
  box_open          开箱 {box}
  transfer          单介质转移（仅未封箱时）{medium,new_custodian,new_witness,to_location}
  handover_confirm  整箱交接确认 {handover,side:out|in,box,generation,media:[...],
                                  new_custodian,new_witness,to_location}
  correction        双签纠错 {correction,snapshot,box,diff_kind,evidence,fix:{set:{...}},signers:[a,b]}

规则要点：
- 主保管人与见证人不得同组（登记、装箱、交接、纠错均校验）。
- 封箱期间介质不能单独转移（transfer 拒绝）。
- 交接双方确认可乱序到达；只有一方时持有人不变；双方到齐才原子生效。
- 首个确认建立预约，记录每件介质的前序证明 ID；第二个确认到达时任一前序
  已变更，则整笔交接记为过时并释放预约。
- 补发相同确认幂等；同一 ID 内容冲突拒绝整批。
- 开箱重封生成新代，旧代确认永远不能用于新代。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3


class Reject(Exception):
    """批次级拒绝：整批回滚。"""


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def proof_id(tx: str, medium_id: str) -> str:
    return f"{tx}:{medium_id}"


# ---------------------------------------------------------------- 批次导入

def import_batch(conn: sqlite3.Connection, batch: dict) -> dict:
    batch_id = batch.get("batch_id")
    events = batch.get("events")
    if not batch_id or not isinstance(events, list) or not events:
        raise Reject("batch 必须包含 batch_id 与非空 events 列表")
    h = content_hash({"batch_id": batch_id, "events": events})
    row = conn.execute("SELECT content_hash FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if row:
        if row["content_hash"] == h:
            return {"batch": batch_id, "status": "duplicate", "events": 0}
        raise Reject(f"批次 {batch_id} 已以不同内容应用过，拒绝整批")

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO batches (batch_id, content_hash) VALUES (?,?)", (batch_id, h))
        for i, ev in enumerate(events):
            if not isinstance(ev, dict) or "type" not in ev:
                raise Reject(f"批次 {batch_id} 第 {i} 个事件缺少 type")
            tx = f"{batch_id}#{i:03d}"
            conn.execute(
                "INSERT INTO events (tx, batch_id, type, payload) VALUES (?,?,?,?)",
                (tx, batch_id, ev["type"], canonical(ev)),
            )
            apply_event(conn, tx, ev)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"batch": batch_id, "status": "applied", "events": len(events)}


def apply_event(conn: sqlite3.Connection, tx: str, ev: dict) -> None:
    handler = _HANDLERS.get(ev["type"])
    if handler is None:
        raise Reject(f"未知事件类型: {ev['type']}")
    handler(conn, tx, ev)


# ---------------------------------------------------------------- 工具

def _person(conn, pid):
    row = conn.execute("SELECT * FROM people WHERE person_id=?", (pid,)).fetchone()
    if row is None:
        raise Reject(f"未知人员: {pid}")
    return row


def _pair_check(conn, custodian: str, witness: str) -> None:
    if custodian == witness:
        raise Reject(f"主保管人与见证人不得为同一人: {custodian}")
    c = _person(conn, custodian)
    w = _person(conn, witness)
    if c["grp"] == w["grp"]:
        raise Reject(f"主保管人 {custodian} 与见证人 {witness} 不得隶属同一组 {c['grp']}")


def _medium(conn, mid):
    row = conn.execute("SELECT * FROM media WHERE medium_id=?", (mid,)).fetchone()
    if row is None:
        raise Reject(f"未知介质: {mid}")
    return row


def _box(conn, box_id):
    row = conn.execute("SELECT * FROM boxes WHERE box_id=?", (box_id,)).fetchone()
    if row is None:
        raise Reject(f"未知箱号: {box_id}")
    return row


def _add_proof(conn, tx, medium_row, kind, box=None, generation=None,
               broken=0, break_reason=None, prior=None):
    pid = proof_id(tx, medium_row["medium_id"])
    conn.execute(
        "INSERT INTO proofs (proof_id, medium_id, seq, tx, kind, box, generation,"
        " custodian, witness, location, prior, broken, break_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, medium_row["medium_id"], _event_seq(conn, tx), tx, kind, box, generation,
         medium_row["custodian"], medium_row["witness"], medium_row["location"],
         medium_row["current_proof"] if prior is None else prior, broken, break_reason),
    )
    conn.execute("UPDATE media SET current_proof=? WHERE medium_id=?",
                 (pid, medium_row["medium_id"]))
    return pid


def _event_seq(conn, tx) -> int:
    return conn.execute("SELECT seq FROM events WHERE tx=?", (tx,)).fetchone()["seq"]


# ---------------------------------------------------------------- 事件处理器

def _ev_register_people(conn, tx, ev):
    for p in ev.get("people", []):
        row = conn.execute("SELECT * FROM people WHERE person_id=?", (p["id"],)).fetchone()
        if row:
            if row["name"] != p["name"] or row["grp"] != p["group"]:
                raise Reject(f"人员 {p['id']} 重复登记且内容冲突")
            continue
        conn.execute("INSERT INTO people (person_id, name, grp) VALUES (?,?,?)",
                     (p["id"], p["name"], p["group"]))


def _ev_register_media(conn, tx, ev):
    for m in ev.get("media", []):
        row = conn.execute("SELECT * FROM media WHERE medium_id=?", (m["id"],)).fetchone()
        if row:
            same = (row["custodian"] == m["custodian"] and row["witness"] == m["witness"]
                    and row["location"] == m["location"])
            if not same:
                raise Reject(f"介质 {m['id']} 重复登记且内容冲突")
            continue
        _pair_check(conn, m["custodian"], m["witness"])
        conn.execute(
            "INSERT INTO media (medium_id, custodian, witness, location, box, box_gen,"
            " current_proof) VALUES (?,?,?,?,NULL,NULL,?)",
            (m["id"], m["custodian"], m["witness"], m["location"], proof_id(tx, m["id"])),
        )
        conn.execute(
            "INSERT INTO proofs (proof_id, medium_id, seq, tx, kind, box, generation,"
            " custodian, witness, location, prior, broken, break_reason)"
            " VALUES (?,?,?,?, 'register', NULL, NULL, ?,?,?,NULL,0,NULL)",
            (proof_id(tx, m["id"]), m["id"], _event_seq(conn, tx), tx,
             m["custodian"], m["witness"], m["location"]),
        )


def _ev_box_seal(conn, tx, ev):
    box_id = ev["box"]
    media_ids = ev["media"]
    if not media_ids:
        raise Reject(f"装箱事件 {box_id} 介质清单为空")
    _pair_check(conn, ev["custodian"], ev["witness"])
    box = conn.execute("SELECT * FROM boxes WHERE box_id=?", (box_id,)).fetchone()
    if box is None:
        generation = 1
    else:
        if box["state"] != "open":
            raise Reject(f"箱 {box_id} 处于封存状态（第 {box['generation']} 代），须先开箱")
        generation = box["generation"] + 1
    for mid in media_ids:
        m = _medium(conn, mid)
        if m["box"] is not None:
            raise Reject(f"介质 {mid} 仍封存在箱 {m['box']} 第 {m['box_gen']} 代，不能单独装箱")
    if box is None:
        conn.execute(
            "INSERT INTO boxes (box_id, generation, seal, custodian, witness, location, state)"
            " VALUES (?,?,?,?,?,?, 'sealed')",
            (box_id, generation, ev["seal"], ev["custodian"], ev["witness"], ev["location"]),
        )
    else:
        conn.execute(
            "UPDATE boxes SET generation=?, seal=?, custodian=?, witness=?, location=?,"
            " state='sealed' WHERE box_id=?",
            (generation, ev["seal"], ev["custodian"], ev["witness"], ev["location"], box_id),
        )
    kind = "seal" if generation == 1 else "reseal"
    for mid in media_ids:
        conn.execute(
            "UPDATE media SET box=?, box_gen=?, custodian=?, witness=?, location=?"
            " WHERE medium_id=?",
            (box_id, generation, ev["custodian"], ev["witness"], ev["location"], mid),
        )
        _add_proof(conn, tx, _medium(conn, mid), kind, box=box_id, generation=generation)


def _ev_box_open(conn, tx, ev):
    box = _box(conn, ev["box"])
    if box["state"] != "sealed":
        raise Reject(f"箱 {ev['box']} 未处于封存状态，不能开箱")
    conn.execute("UPDATE boxes SET state='open' WHERE box_id=?", (ev["box"],))
    rows = conn.execute("SELECT * FROM media WHERE box=?", (ev["box"],)).fetchall()
    for m in rows:
        conn.execute("UPDATE media SET box=NULL, box_gen=NULL WHERE medium_id=?",
                     (m["medium_id"],))
        _add_proof(conn, tx, _medium(conn, m["medium_id"]), "open",
                   box=ev["box"], generation=box["generation"])


def _ev_transfer(conn, tx, ev):
    m = _medium(conn, ev["medium"])
    if m["box"] is not None:
        raise Reject(f"介质 {m['medium_id']} 封存在箱 {m['box']} 第 {m['box_gen']} 代，"
                     "封箱期间不能单独转移")
    _pair_check(conn, ev["new_custodian"], ev["new_witness"])
    conn.execute(
        "UPDATE media SET custodian=?, witness=?, location=? WHERE medium_id=?",
        (ev["new_custodian"], ev["new_witness"], ev["to_location"], m["medium_id"]),
    )
    _add_proof(conn, tx, _medium(conn, m["medium_id"]), "transfer")


def _handover_content(ev) -> dict:
    return {
        "box": ev["box"],
        "generation": ev["generation"],
        "media": sorted(ev["media"]),
        "new_custodian": ev["new_custodian"],
        "new_witness": ev["new_witness"],
        "to_location": ev["to_location"],
    }


def _ev_handover_confirm(conn, tx, ev):
    hid = ev["handover"]
    side = ev.get("side")
    if side not in ("out", "in"):
        raise Reject(f"交接 {hid} 确认方 side 必须为 out 或 in")
    content = _handover_content(ev)
    ch = content_hash(content)

    row = conn.execute("SELECT * FROM handovers WHERE handover_id=?", (hid,)).fetchone()
    if row:
        if row["content_hash"] != ch:
            raise Reject(f"交接 {hid} 已存在且确认内容冲突，拒绝整批")
        col = "out_confirmed" if side == "out" else "in_confirmed"
        if row[col]:
            return  # 补发相同确认：幂等
        if row["status"] != "pending":
            return  # 已完结/已过时的交接，同内容确认不再改变状态
        conn.execute(f"UPDATE handovers SET {col}=1 WHERE handover_id=?", (hid,))
        _try_complete_handover(conn, tx, hid)
        return

    # 首个确认：建立预约，记录每件介质的前序证明 ID
    _pair_check(conn, content["new_custodian"], content["new_witness"])
    box = _box(conn, content["box"])
    for mid in content["media"]:
        _medium(conn, mid)
    stale_reason = None
    if box["state"] != "sealed" or box["generation"] != content["generation"]:
        stale_reason = (f"代次不符：箱 {content['box']} 当前为第 {box['generation']} 代"
                        f"（{box['state']}），确认针对第 {content['generation']} 代，"
                        "旧代确认永远不能用于新代")
    else:
        current = sorted(r["medium_id"] for r in conn.execute(
            "SELECT medium_id FROM media WHERE box=?", (content["box"],)).fetchall())
        if current != content["media"]:
            stale_reason = "确认清单与箱内实际介质不符"
    status = "stale" if stale_reason else "pending"
    conn.execute(
        "INSERT INTO handovers (handover_id, content_hash, box, generation, media,"
        " new_custodian, new_witness, to_location, out_confirmed, in_confirmed,"
        " status, stale_reason, created_tx) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (hid, ch, content["box"], content["generation"], canonical(content["media"]),
         content["new_custodian"], content["new_witness"], content["to_location"],
         1 if side == "out" else 0, 1 if side == "in" else 0,
         status, stale_reason, tx),
    )
    if status == "pending":
        for mid in content["media"]:
            m = _medium(conn, mid)
            conn.execute(
                "INSERT INTO reservations (handover_id, medium_id, prior_proof) VALUES (?,?,?)",
                (hid, mid, m["current_proof"]),
            )


def _try_complete_handover(conn, tx, hid):
    row = conn.execute("SELECT * FROM handovers WHERE handover_id=?", (hid,)).fetchone()
    if not (row["out_confirmed"] and row["in_confirmed"]):
        return
    media_list = json.loads(row["media"])
    box = _box(conn, row["box"])
    reason = None
    if box["state"] != "sealed" or box["generation"] != row["generation"]:
        reason = (f"代次已被取代：箱 {row['box']} 当前第 {box['generation']} 代，"
                  f"交接针对第 {row['generation']} 代")
    else:
        current = sorted(r["medium_id"] for r in conn.execute(
            "SELECT medium_id FROM media WHERE box=?", (row["box"],)).fetchall())
        if current != media_list:
            reason = "箱内介质清单已变化"
        else:
            for mid in media_list:
                res = conn.execute(
                    "SELECT prior_proof FROM reservations WHERE handover_id=? AND medium_id=?",
                    (hid, mid)).fetchone()
                cur = _medium(conn, mid)["current_proof"]
                if res is None or res["prior_proof"] != cur:
                    reason = f"介质 {mid} 的前序证明已变更，整笔交接过时"
                    break
    if reason:
        conn.execute("UPDATE handovers SET status='stale', stale_reason=?, resolved_tx=?"
                     " WHERE handover_id=?", (reason, tx, hid))
        conn.execute("DELETE FROM reservations WHERE handover_id=?", (hid,))
        return
    # 原子生效：箱与箱内全部介质同时改变归属
    conn.execute(
        "UPDATE boxes SET custodian=?, witness=?, location=? WHERE box_id=?",
        (row["new_custodian"], row["new_witness"], row["to_location"], row["box"]),
    )
    for mid in media_list:
        conn.execute(
            "UPDATE media SET custodian=?, witness=?, location=? WHERE medium_id=?",
            (row["new_custodian"], row["new_witness"], row["to_location"], mid),
        )
        _add_proof(conn, tx, _medium(conn, mid), "handover",
                   box=row["box"], generation=row["generation"])
    conn.execute("UPDATE handovers SET status='completed', resolved_tx=? WHERE handover_id=?",
                 (tx, hid))
    conn.execute("DELETE FROM reservations WHERE handover_id=?", (hid,))


def _ev_correction(conn, tx, ev):
    cid = ev["correction"]
    body = {k: ev[k] for k in ("snapshot", "box", "diff_kind", "evidence", "fix", "signers")}
    ch = content_hash(body)
    row = conn.execute("SELECT content_hash FROM corrections WHERE correction_id=?",
                       (cid,)).fetchone()
    if row:
        if row["content_hash"] != ch:
            raise Reject(f"纠错 {cid} 已存在且内容冲突，拒绝整批")
        return  # 幂等

    snap = conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?",
                        (ev["snapshot"],)).fetchone()
    if snap is None:
        raise Reject(f"纠错 {cid} 引用的盘点 {ev['snapshot']} 不存在")
    latest = conn.execute(
        "SELECT snapshot_id FROM snapshots ORDER BY seq DESC LIMIT 1").fetchone()
    if latest and latest["snapshot_id"] != ev["snapshot"]:
        raise Reject(f"纠错 {cid} 引用的是旧盘点 {ev['snapshot']}，"
                     f"最新盘点为 {latest['snapshot_id']}，禁止把后来的纠错倒填到旧盘点")
    diff = conn.execute(
        "SELECT * FROM diffs WHERE snapshot_id=? AND box=? AND kind=?",
        (ev["snapshot"], ev["box"], ev["diff_kind"])).fetchone()
    if diff is None:
        raise Reject(f"纠错 {cid} 引用的差异 ({ev['snapshot']}, {ev['box']}, "
                     f"{ev['diff_kind']}) 不存在")

    signers = ev["signers"]
    if len(signers) != 2 or signers[0] == signers[1]:
        raise Reject(f"纠错 {cid} 需要两名不同的独立签名人")
    _pair_check(conn, signers[0], signers[1])

    box = _box(conn, ev["box"])
    setf = ev.get("fix", {}).get("set", {})
    new_cust = setf.get("custodian", box["custodian"])
    new_wit = setf.get("witness", box["witness"])
    new_loc = setf.get("location", box["location"])
    _pair_check(conn, new_cust, new_wit)

    conn.execute(
        "UPDATE boxes SET custodian=?, witness=?, location=? WHERE box_id=?",
        (new_cust, new_wit, new_loc, ev["box"]),
    )
    if setf:
        # 纠错绕过正常交接改变记录状态：箱内介质的自动证据链在此断开，
        # 纠错本身（证据 + 双签）成为断点处的衔接凭证。
        reason = f"correction {cid}: {ev['evidence']}"
        for m in conn.execute("SELECT * FROM media WHERE box=?", (ev["box"],)).fetchall():
            conn.execute(
                "UPDATE media SET custodian=?, witness=?, location=? WHERE medium_id=?",
                (new_cust, new_wit, new_loc, m["medium_id"]),
            )
            _add_proof(conn, tx, _medium(conn, m["medium_id"]), "correction",
                       box=ev["box"], generation=box["generation"],
                       broken=1, break_reason=reason)
    conn.execute(
        "INSERT INTO corrections (correction_id, content_hash, snapshot_id, box, diff_kind,"
        " evidence, fix, signers, tx) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, ch, ev["snapshot"], ev["box"], ev["diff_kind"], ev["evidence"],
         canonical(ev.get("fix", {})), canonical(signers), tx),
    )


_HANDLERS = {
    "register_people": _ev_register_people,
    "register_media": _ev_register_media,
    "box_seal": _ev_box_seal,
    "box_open": _ev_box_open,
    "transfer": _ev_transfer,
    "handover_confirm": _ev_handover_confirm,
    "correction": _ev_correction,
}


# ---------------------------------------------------------------- 盘点

def record_inventory(conn: sqlite3.Connection, snap: dict) -> dict:
    sid = snap.get("snapshot_id")
    location = snap.get("location")
    observed = snap.get("observed")
    if not sid or not location or not isinstance(observed, list):
        raise Reject("盘点快照必须包含 snapshot_id、location 与 observed 列表")
    seen = set()
    for item in observed:
        if item["box"] in seen:
            raise Reject(f"盘点 {sid} 中箱号 {item['box']} 重复出现")
        seen.add(item["box"])
    ch = content_hash(snap)
    row = conn.execute("SELECT content_hash FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()
    if row:
        if row["content_hash"] != ch:
            raise Reject(f"盘点 {sid} 已存在且内容冲突")
        return _inventory_report(conn, sid, duplicate=True)

    conn.execute("BEGIN IMMEDIATE")
    try:
        tx = f"inventory:{sid}"
        conn.execute(
            "INSERT INTO events (tx, batch_id, type, payload) VALUES (?, NULL, 'inventory', ?)",
            (tx, canonical(snap)),
        )
        seq = _event_seq(conn, tx)
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, content_hash, location, seq) VALUES (?,?,?,?)",
            (sid, ch, location, seq),
        )
        for item in observed:
            conn.execute(
                "INSERT INTO snapshot_items (snapshot_id, box, generation, seal)"
                " VALUES (?,?,?,?)",
                (sid, item["box"], item["generation"], item["seal"]),
            )
        expected = {r["box_id"]: (r["generation"], r["seal"]) for r in conn.execute(
            "SELECT box_id, generation, seal FROM boxes WHERE location=?", (location,))}
        obs = {i["box"]: (i["generation"], i["seal"]) for i in observed}
        for box_id in sorted(expected.keys() - obs.keys()):
            g, s = expected[box_id]
            conn.execute(
                "INSERT INTO diffs (snapshot_id, kind, box, expected, observed)"
                " VALUES (?, 'missing', ?, ?, NULL)",
                (sid, box_id, canonical({"generation": g, "seal": s})))
        for box_id in sorted(obs.keys() - expected.keys()):
            g, s = obs[box_id]
            conn.execute(
                "INSERT INTO diffs (snapshot_id, kind, box, expected, observed)"
                " VALUES (?, 'extra', ?, NULL, ?)",
                (sid, box_id, canonical({"generation": g, "seal": s})))
        for box_id in sorted(expected.keys() & obs.keys()):
            if expected[box_id] != obs[box_id]:
                g1, s1 = expected[box_id]
                g2, s2 = obs[box_id]
                conn.execute(
                    "INSERT INTO diffs (snapshot_id, kind, box, expected, observed)"
                    " VALUES (?, 'wrong_generation', ?, ?, ?)",
                    (sid, box_id, canonical({"generation": g1, "seal": s1}),
                     canonical({"generation": g2, "seal": s2})))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return _inventory_report(conn, sid, duplicate=False)


def _inventory_report(conn, sid, duplicate):
    snap = conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()
    report = {"snapshot": sid, "location": snap["location"], "duplicate": duplicate,
              "missing": [], "extra": [], "wrong_generation": []}
    for d in conn.execute(
            "SELECT * FROM diffs WHERE snapshot_id=? ORDER BY box", (sid,)).fetchall():
        entry = {"box": d["box"]}
        if d["expected"]:
            entry["expected"] = json.loads(d["expected"])
        if d["observed"]:
            entry["observed"] = json.loads(d["observed"])
        report[d["kind"]].append(entry)
    return report


# ---------------------------------------------------------------- 证据链解释

def explain(conn: sqlite3.Connection, medium_id: str, to_tx: str | None = None) -> dict:
    m = conn.execute("SELECT * FROM media WHERE medium_id=?", (medium_id,)).fetchone()
    if m is None:
        raise Reject(f"未知介质: {medium_id}")
    max_seq = None
    if to_tx is not None:
        row = conn.execute("SELECT seq FROM events WHERE tx=?", (to_tx,)).fetchone()
        if row is None:
            raise Reject(f"未知事务: {to_tx}")
        max_seq = row["seq"]
    q = "SELECT * FROM proofs WHERE medium_id=?"
    params = [medium_id]
    if max_seq is not None:
        q += " AND seq<=?"
        params.append(max_seq)
    q += " ORDER BY seq"
    proofs = conn.execute(q, params).fetchall()

    chain = []
    first_break = None
    prev = None
    for p in proofs:
        entry = {
            "seq": p["seq"], "tx": p["tx"], "kind": p["kind"],
            "box": p["box"], "generation": p["generation"],
            "custodian": p["custodian"], "witness": p["witness"],
            "location": p["location"],
            "proof": p["proof_id"], "prior": p["prior"],
            "broken": bool(p["broken"]),
        }
        chain.append(entry)
        if first_break is None:
            if p["broken"]:
                first_break = {
                    "tx": p["tx"], "kind": p["kind"], "reason": p["break_reason"],
                    "box": p["box"], "generation": p["generation"],
                }
            elif prev is not None and p["prior"] != prev["proof_id"]:
                first_break = {
                    "tx": p["tx"], "kind": p["kind"],
                    "reason": f"前序证明不匹配：期望 {prev['proof_id']}，实际 {p['prior']}",
                    "box": p["box"], "generation": p["generation"],
                }
        prev = p
    return {
        "medium": medium_id,
        "to": to_tx,
        "continuous": first_break is None,
        "first_break": first_break,
        "chain": chain,
    }

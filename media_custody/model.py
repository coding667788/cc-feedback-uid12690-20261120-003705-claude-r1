"""独立小模型：verify 命令使用的朴素回放实现。

刻意不复用 core.py 的任何函数：用普通字典按事件日志顺序重放全部规则，
再与库中物化状态、历史盘点差异、证明链逐一比对。两套实现互相印证，
任何一边出错（或库被篡改）都会以 mismatch 形式暴露。
"""

from __future__ import annotations

import json
import sqlite3


def _pid(tx, medium):
    return f"{tx}:{medium}"


def replay(events):
    """events: [(seq, tx, type, payload_dict), ...] 按 seq 升序。

    返回 {"media": {...}, "boxes": {...}, "snapshots": [...]}。
    遇到不合法事件直接抛 ModelError —— 库中只应存在已通过校验的事件。
    """
    people = {}
    media = {}
    boxes = {}
    handovers = {}
    snapshots = []

    def person(pid):
        if pid not in people:
            raise ModelError(f"未知人员 {pid}")
        return people[pid]

    def pair_ok(c, w):
        if c == w or person(c)["group"] == person(w)["group"]:
            raise ModelError(f"共管组合不合法: {c}/{w}")

    for seq, tx, typ, p in events:
        if typ == "register_people":
            for x in p["people"]:
                people.setdefault(x["id"], {"name": x["name"], "group": x["group"]})
        elif typ == "register_media":
            for x in p["media"]:
                if x["id"] in media:
                    continue
                pair_ok(x["custodian"], x["witness"])
                media[x["id"]] = {
                    "custodian": x["custodian"], "witness": x["witness"],
                    "location": x["location"], "box": None, "box_gen": None,
                    "proof": _pid(tx, x["id"]),
                }
        elif typ == "box_seal":
            pair_ok(p["custodian"], p["witness"])
            b = boxes.get(p["box"])
            gen = 1 if b is None else b["generation"] + 1
            if b is not None and b["state"] != "open":
                raise ModelError(f"箱 {p['box']} 未开箱就重封")
            boxes[p["box"]] = {
                "generation": gen, "seal": p["seal"], "custodian": p["custodian"],
                "witness": p["witness"], "location": p["location"], "state": "sealed",
            }
            for mid in p["media"]:
                m = media[mid]
                if m["box"] is not None:
                    raise ModelError(f"介质 {mid} 仍封存在 {m['box']}")
                m.update(box=p["box"], box_gen=gen, custodian=p["custodian"],
                         witness=p["witness"], location=p["location"],
                         proof=_pid(tx, mid))
        elif typ == "box_open":
            b = boxes[p["box"]]
            if b["state"] != "sealed":
                raise ModelError(f"箱 {p['box']} 未封存")
            b["state"] = "open"
            for mid, m in media.items():
                if m["box"] == p["box"]:
                    m.update(box=None, box_gen=None, proof=_pid(tx, mid))
        elif typ == "transfer":
            m = media[p["medium"]]
            if m["box"] is not None:
                raise ModelError(f"介质 {p['medium']} 封存中，不能单独转移")
            pair_ok(p["new_custodian"], p["new_witness"])
            m.update(custodian=p["new_custodian"], witness=p["new_witness"],
                     location=p["to_location"], proof=_pid(tx, p["medium"]))
        elif typ == "handover_confirm":
            content = json.dumps({
                "box": p["box"], "generation": p["generation"],
                "media": sorted(p["media"]), "new_custodian": p["new_custodian"],
                "new_witness": p["new_witness"], "to_location": p["to_location"],
            }, sort_keys=True)
            h = handovers.get(p["handover"])
            if h is None:
                pair_ok(p["new_custodian"], p["new_witness"])
                h = {"content": content, "box": p["box"], "generation": p["generation"],
                     "media": sorted(p["media"]), "new_custodian": p["new_custodian"],
                     "new_witness": p["new_witness"], "to_location": p["to_location"],
                     "sides": set(), "status": "pending", "priors": {}}
                b = boxes[p["box"]]
                current = sorted(mid for mid, m in media.items() if m["box"] == p["box"])
                if b["state"] != "sealed" or b["generation"] != p["generation"] \
                        or current != h["media"]:
                    h["status"] = "stale"
                else:
                    for mid in h["media"]:
                        h["priors"][mid] = media[mid]["proof"]
                handovers[p["handover"]] = h
            else:
                if h["content"] != content:
                    raise ModelError(f"交接 {p['handover']} 内容冲突")
            if h["status"] == "pending":
                h["sides"].add(p["side"])
                if h["sides"] == {"out", "in"}:
                    _complete(h, boxes, media, tx)
        elif typ == "correction":
            setf = p.get("fix", {}).get("set", {})
            b = boxes[p["box"]]
            b["custodian"] = setf.get("custodian", b["custodian"])
            b["witness"] = setf.get("witness", b["witness"])
            b["location"] = setf.get("location", b["location"])
            if setf:
                for mid, m in media.items():
                    if m["box"] == p["box"]:
                        m.update(custodian=b["custodian"], witness=b["witness"],
                                 location=b["location"], proof=_pid(tx, mid))
        elif typ == "inventory":
            expected = {bid: (b["generation"], b["seal"])
                        for bid, b in boxes.items() if b["location"] == p["location"]}
            obs = {i["box"]: (i["generation"], i["seal"]) for i in p["observed"]}
            diffs = []
            for bid in sorted(expected.keys() - obs.keys()):
                g, s = expected[bid]
                diffs.append(("missing", bid, {"generation": g, "seal": s}, None))
            for bid in sorted(obs.keys() - expected.keys()):
                g, s = obs[bid]
                diffs.append(("extra", bid, None, {"generation": g, "seal": s}))
            for bid in sorted(expected.keys() & obs.keys()):
                if expected[bid] != obs[bid]:
                    g1, s1 = expected[bid]
                    g2, s2 = obs[bid]
                    diffs.append(("wrong_generation", bid,
                                  {"generation": g1, "seal": s1},
                                  {"generation": g2, "seal": s2}))
            snapshots.append({"snapshot_id": p["snapshot_id"], "diffs": diffs})
        else:
            raise ModelError(f"未知事件类型 {typ}")
    return {"media": media, "boxes": boxes, "snapshots": snapshots}


def _complete(h, boxes, media, tx):
    b = boxes[h["box"]]
    current = sorted(mid for mid, m in media.items() if m["box"] == h["box"])
    stale = (b["state"] != "sealed" or b["generation"] != h["generation"]
             or current != h["media"]
             or any(media[mid]["proof"] != h["priors"].get(mid) for mid in h["media"]))
    if stale:
        h["status"] = "stale"
        return
    h["status"] = "completed"
    b.update(custodian=h["new_custodian"], witness=h["new_witness"],
             location=h["to_location"])
    for mid in h["media"]:
        media[mid].update(custodian=h["new_custodian"], witness=h["new_witness"],
                          location=h["to_location"], proof=_pid(tx, mid))


class ModelError(Exception):
    pass


# ---------------------------------------------------------------- 比对

def verify(conn: sqlite3.Connection) -> dict:
    events = [
        (r["seq"], r["tx"], r["type"], json.loads(r["payload"]))
        for r in conn.execute("SELECT seq, tx, type, payload FROM events ORDER BY seq")
    ]
    mismatches = []
    try:
        st = replay(events)
    except ModelError as exc:
        return {"status": "mismatch", "mismatches": [f"小模型回放失败: {exc}"],
                "checked": {"events": len(events)}}

    # 1) 箱最终状态
    db_boxes = {r["box_id"]: r for r in conn.execute("SELECT * FROM boxes")}
    if set(db_boxes) != set(st["boxes"]):
        mismatches.append(
            f"箱集合不一致: 库={sorted(db_boxes)} 模型={sorted(st['boxes'])}")
    for bid, b in st["boxes"].items():
        r = db_boxes.get(bid)
        if r is None:
            continue
        for col, key in (("generation", "generation"), ("seal", "seal"),
                         ("custodian", "custodian"), ("witness", "witness"),
                         ("location", "location"), ("state", "state")):
            if r[col] != b[key]:
                mismatches.append(f"箱 {bid} 字段 {col}: 库={r[col]} 模型={b[key]}")

    # 2) 介质最终状态（含当前证明 ID）
    db_media = {r["medium_id"]: r for r in conn.execute("SELECT * FROM media")}
    if set(db_media) != set(st["media"]):
        mismatches.append(
            f"介质集合不一致: 库={sorted(db_media)} 模型={sorted(st['media'])}")
    for mid, m in st["media"].items():
        r = db_media.get(mid)
        if r is None:
            continue
        pairs = (("custodian", "custodian"), ("witness", "witness"),
                 ("location", "location"), ("box", "box"),
                 ("box_gen", "box_gen"), ("current_proof", "proof"))
        for col, key in pairs:
            if r[col] != m[key]:
                mismatches.append(f"介质 {mid} 字段 {col}: 库={r[col]} 模型={m[key]}")

    # 3) 全部历史盘点差异
    for snap in st["snapshots"]:
        sid = snap["snapshot_id"]
        stored = set()
        for d in conn.execute("SELECT * FROM diffs WHERE snapshot_id=?", (sid,)):
            stored.add((d["kind"], d["box"], d["expected"], d["observed"]))
        model = set()
        for kind, bid, exp, obs in snap["diffs"]:
            model.add((kind, bid,
                       json.dumps(exp, sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":")) if exp else None,
                       json.dumps(obs, sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":")) if obs else None))
        if stored != model:
            mismatches.append(
                f"盘点 {sid} 差异不一致: 库={sorted(stored)} 模型={sorted(model)}")

    # 4) 证明链完整性（每条介质：首条 prior 为空，其后 prior 链接上一条，
    #    断链处必须带 broken 标记与原因）
    for mid in db_media:
        prev = None
        for p in conn.execute(
                "SELECT * FROM proofs WHERE medium_id=? ORDER BY seq", (mid,)):
            if prev is None:
                if p["prior"] is not None:
                    mismatches.append(f"介质 {mid} 首条证明 {p['proof_id']} prior 非空")
            else:
                if p["prior"] != prev["proof_id"] and not p["broken"]:
                    mismatches.append(
                        f"介质 {mid} 证明 {p['proof_id']} 前序断裂且未标记")
            if p["broken"] and not p["break_reason"]:
                mismatches.append(f"介质 {mid} 证明 {p['proof_id']} 断链缺原因")
            prev = p

    return {
        "status": "ok" if not mismatches else "mismatch",
        "checked": {
            "events": len(events),
            "boxes": len(st["boxes"]),
            "media": len(st["media"]),
            "snapshots": len(st["snapshots"]),
        },
        "mismatches": mismatches,
    }

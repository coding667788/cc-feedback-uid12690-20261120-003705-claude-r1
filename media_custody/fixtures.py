"""确定性夹具生成：三箱交叉移交演示场景。

同一命令重复执行产生字节级一致的文件（无时间戳、无随机数），
全部实体均为虚构。场景：

  batch_001  登记 6 人（3 组）与 6 件介质
  batch_002  装箱：BX01@LAB-A、BX02@LAB-B、BX03@LAB-C（均为第 1 代）
  snap_001   盘点 LAB-A（移交前基线）
  batch_003  三笔交接的首个确认（乱序、出/入混合）
  batch_004  三笔交接的第二个确认 → 交叉移交原子生效
  snap_002/003/004  盘点移交后的三个地点（snap_004 故意错报 BX02 代次）
  batch_005  双签纠错 C001：引用 snap_004 的错代差异，证据为现场复核
  snap_005   纠错后复盘 LAB-C
"""

from __future__ import annotations

import json
import os

PEOPLE = [
    {"id": "P01", "name": "张岚", "group": "G1"},
    {"id": "P02", "name": "陈默", "group": "G1"},
    {"id": "P03", "name": "李航", "group": "G2"},
    {"id": "P04", "name": "赵颖", "group": "G2"},
    {"id": "P05", "name": "王硕", "group": "G3"},
    {"id": "P06", "name": "孙莉", "group": "G3"},
]

MEDIA = [
    {"id": "M001", "custodian": "P01", "witness": "P03", "location": "LAB-A"},
    {"id": "M002", "custodian": "P01", "witness": "P04", "location": "LAB-A"},
    {"id": "M003", "custodian": "P03", "witness": "P05", "location": "LAB-B"},
    {"id": "M004", "custodian": "P03", "witness": "P06", "location": "LAB-B"},
    {"id": "M005", "custodian": "P05", "witness": "P01", "location": "LAB-C"},
    {"id": "M006", "custodian": "P05", "witness": "P02", "location": "LAB-C"},
]

BOXES = [
    {"box": "BX01", "seal": "SEAL-BX01-1", "media": ["M001", "M002"],
     "custodian": "P01", "witness": "P03", "location": "LAB-A"},
    {"box": "BX02", "seal": "SEAL-BX02-1", "media": ["M003", "M004"],
     "custodian": "P03", "witness": "P05", "location": "LAB-B"},
    {"box": "BX03", "seal": "SEAL-BX03-1", "media": ["M005", "M006"],
     "custodian": "P05", "witness": "P01", "location": "LAB-C"},
]

# 三箱交叉移交：BX01→LAB-B、BX02→LAB-C、BX03→LAB-A
HANDOVERS = [
    {"handover": "H001", "box": "BX01", "generation": 1, "media": ["M001", "M002"],
     "new_custodian": "P03", "new_witness": "P05", "to_location": "LAB-B"},
    {"handover": "H002", "box": "BX02", "generation": 1, "media": ["M003", "M004"],
     "new_custodian": "P05", "new_witness": "P01", "to_location": "LAB-C"},
    {"handover": "H003", "box": "BX03", "generation": 1, "media": ["M005", "M006"],
     "new_custodian": "P02", "new_witness": "P04", "to_location": "LAB-A"},
]


def _confirm(h, side):
    return {"type": "handover_confirm", "side": side, **h}


def build_files() -> dict:
    """返回 {文件名: JSON 对象}，内容完全确定。"""
    files = {}
    files["batch_001_register.json"] = {
        "batch_id": "B001",
        "events": [
            {"type": "register_people", "people": PEOPLE},
            {"type": "register_media", "media": MEDIA},
        ],
    }
    files["batch_002_boxing.json"] = {
        "batch_id": "B002",
        "events": [{"type": "box_seal", **b} for b in BOXES],
    }
    files["snap_001_lab_a_before.json"] = {
        "snapshot_id": "SNAP001", "location": "LAB-A",
        "observed": [{"box": "BX01", "generation": 1, "seal": "SEAL-BX01-1"}],
    }
    # 首个确认乱序到达：H001 出方、H002 入方、H003 出方
    files["batch_003_first_confirms.json"] = {
        "batch_id": "B003",
        "events": [_confirm(HANDOVERS[0], "out"),
                   _confirm(HANDOVERS[1], "in"),
                   _confirm(HANDOVERS[2], "out")],
    }
    # 第二个确认到齐，三笔交叉移交原子生效
    files["batch_004_second_confirms.json"] = {
        "batch_id": "B004",
        "events": [_confirm(HANDOVERS[0], "in"),
                   _confirm(HANDOVERS[1], "out"),
                   _confirm(HANDOVERS[2], "in")],
    }
    files["snap_002_lab_a_after.json"] = {
        "snapshot_id": "SNAP002", "location": "LAB-A",
        "observed": [{"box": "BX03", "generation": 1, "seal": "SEAL-BX03-1"}],
    }
    files["snap_003_lab_b_after.json"] = {
        "snapshot_id": "SNAP003", "location": "LAB-B",
        "observed": [{"box": "BX01", "generation": 1, "seal": "SEAL-BX01-1"}],
    }
    # 盘点员把 BX02 的代次误记为 2 → 产生 wrong_generation 差异
    files["snap_004_lab_c_misread.json"] = {
        "snapshot_id": "SNAP004", "location": "LAB-C",
        "observed": [{"box": "BX02", "generation": 2, "seal": "SEAL-BX02-2"}],
    }
    files["batch_005_correction.json"] = {
        "batch_id": "B005",
        "events": [{
            "type": "correction",
            "correction": "C001",
            "snapshot": "SNAP004",
            "box": "BX02",
            "diff_kind": "wrong_generation",
            "evidence": "盘点员误读封签，现场复核确认 BX02 仍为第 1 代 SEAL-BX02-1",
            "fix": {"set": {}},
            "signers": ["P01", "P04"],
        }],
    }
    files["snap_005_lab_c_recount.json"] = {
        "snapshot_id": "SNAP005", "location": "LAB-C",
        "observed": [{"box": "BX02", "generation": 1, "seal": "SEAL-BX02-1"}],
    }
    files["manifest.json"] = {
        "description": "三箱交叉移交演示场景（全部虚构，确定性生成）",
        "order": [
            "batch_001_register.json",
            "batch_002_boxing.json",
            "snap_001_lab_a_before.json",
            "batch_003_first_confirms.json",
            "batch_004_second_confirms.json",
            "snap_002_lab_a_after.json",
            "snap_003_lab_b_after.json",
            "snap_004_lab_c_misread.json",
            "batch_005_correction.json",
            "snap_005_lab_c_recount.json",
        ],
    }
    return files


def write_fixtures(outdir: str) -> list:
    os.makedirs(outdir, exist_ok=True)
    written = []
    for name, obj in build_files().items():
        path = os.path.join(outdir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        written.append(path)
    return written

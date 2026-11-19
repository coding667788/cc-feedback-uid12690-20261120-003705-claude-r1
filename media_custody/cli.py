"""命令行入口：init / fixtures / import-batch / inventory / explain / verify。"""

from __future__ import annotations

import argparse
import json
import sys

from . import core, db, fixtures, model


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_init(args):
    db.init_db(args.db)
    _print({"status": "ok", "db": args.db})
    return 0


def cmd_fixtures(args):
    written = fixtures.write_fixtures(args.out)
    _print({"status": "ok", "written": written})
    return 0


def cmd_import_batch(args):
    conn = db.connect(args.db)
    try:
        result = core.import_batch(conn, _load_json(args.file))
    except core.Reject as exc:
        _print({"status": "rejected", "reason": str(exc)})
        return 1
    finally:
        conn.close()
    _print(result)
    return 0


def cmd_inventory(args):
    conn = db.connect(args.db)
    try:
        report = core.record_inventory(conn, _load_json(args.file))
    except core.Reject as exc:
        _print({"status": "rejected", "reason": str(exc)})
        return 1
    finally:
        conn.close()
    _print(report)
    return 0 if not any(report[k] for k in ("missing", "extra", "wrong_generation")) else 2


def cmd_explain(args):
    conn = db.connect(args.db)
    try:
        result = core.explain(conn, args.medium, to_tx=args.to)
    except core.Reject as exc:
        _print({"status": "rejected", "reason": str(exc)})
        return 1
    finally:
        conn.close()
    _print(result)
    return 0 if result["continuous"] else 2


def cmd_verify(args):
    conn = db.connect(args.db)
    try:
        result = model.verify(conn)
    finally:
        conn.close()
    _print(result)
    return 0 if result["status"] == "ok" else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="media_custody",
        description="离线介质保全登记工具（虚构封签/箱号，SQLite 本地库）")
    p.add_argument("--db", default="custody.db", help="SQLite 库路径（默认 custody.db）")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化库")

    sp = sub.add_parser("fixtures", help="生成确定性演示夹具")
    sp.add_argument("--out", required=True, help="输出目录")

    sp = sub.add_parser("import-batch", help="导入事件批次（整批原子）")
    sp.add_argument("file", help="批次 JSON 文件")

    sp = sub.add_parser("inventory", help="登记盘点快照并输出差异报告")
    sp.add_argument("file", help="盘点快照 JSON 文件")

    sp = sub.add_parser("explain", help="解释某介质的连续交接证据链")
    sp.add_argument("--medium", required=True, help="介质 ID")
    sp.add_argument("--to", default=None, help="截止事务 ID（含），缺省为最新")

    sub.add_parser("verify", help="用独立小模型重放事件日志并比对全部状态与历史盘点")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    handlers = {
        "init": cmd_init,
        "fixtures": cmd_fixtures,
        "import-batch": cmd_import_batch,
        "inventory": cmd_inventory,
        "explain": cmd_explain,
        "verify": cmd_verify,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

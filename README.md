# 封存介质的共管移交与差异追溯

离线介质保全登记工具。实验室以**虚构**封签与箱号管理不可拆分的介质，支持
共管登记、装箱代次、双方确认交接、盘点差异、双签纠错与证据链解释。
全部输入由本地夹具生成，无实际介质、外部系统或真实事件。

- 运行环境：Windows 原生 Python 3.12，仅标准库 + SQLite，无第三方依赖。
- 题面见 `task/PROMPT.txt`。

## 快速开始

```bat
python -m media_custody fixtures --out data          :: 生成确定性演示夹具
python -m media_custody --db custody.db init         :: 初始化库
python -m media_custody --db custody.db import-batch data\batch_001_register.json
python -m media_custody --db custody.db import-batch data\batch_002_boxing.json
python -m media_custody --db custody.db inventory   data\snap_001_lab_a_before.json
python -m media_custody --db custody.db import-batch data\batch_003_first_confirms.json
python -m media_custody --db custody.db import-batch data\batch_004_second_confirms.json
python -m media_custody --db custody.db inventory   data\snap_002_lab_a_after.json
python -m media_custody --db custody.db inventory   data\snap_003_lab_b_after.json
python -m media_custody --db custody.db inventory   data\snap_004_lab_c_misread.json
python -m media_custody --db custody.db import-batch data\batch_005_correction.json
python -m media_custody --db custody.db inventory   data\snap_005_lab_c_recount.json
python -m media_custody --db custody.db explain --medium M001
python -m media_custody --db custody.db verify
```

`data\manifest.json` 给出了唯一合法顺序（纠错必须引用当时最新的盘点）。
退出码：`0` 正常；`1` 拒绝/校验失败；`2` 盘点有差异或证据链断链（正常业务结果）。

## 命令

| 命令 | 说明 |
| --- | --- |
| `init` | 创建库与模式（幂等） |
| `fixtures --out DIR` | 生成演示夹具，字节级可重现 |
| `import-batch FILE` | 导入事件批次，整批原子：任一事件非法则整批回滚 |
| `inventory FILE` | 登记盘点快照并输出缺失/额外/错代三类差异（按箱号排序） |
| `explain --medium M [--to TX]` | 输出介质从登记到指定事务的连续交接证据与首次断链 |
| `verify` | 用独立小模型重放全部事件，比对当前状态与所有历史盘点 |

## 数据与规则

### 共管

每件介质/每只箱同时由一名主保管人与一名独立见证人共管，二人不得隶属
同一组（登记、装箱、交接、纠错均强制校验）。

### 装箱与代次

`box_seal` 把若干介质绑定到箱封签的某一代次；`box_open` 开箱后再次
`box_seal` 生成新代次与新封签。封箱期间介质不能单独转移（`transfer`
拒绝），开箱后方可单件转移。旧代确认永远不能用于新代。

### 整箱交接（双方确认）

`handover_confirm` 由出方（`out`）与入方（`in`）分别给出**同一**交接编号、
代次与完整介质清单，并附新保管人与新见证人：

- 两个确认可乱序到达；只收到一个时持有人不变，两个到齐才**原子**改变箱内
  全部介质的归属。
- 首个确认建立预约，记录每件介质的前序证明 ID；第二个确认到达时，只要一件
  前序已变更（或代次、箱内清单已变），整笔记为**过时**并释放预约。
- 补发相同确认幂等；同一交接编号内容冲突（如清单错一件）拒绝整批。
- 涉及同一箱的并发批次由 `BEGIN IMMEDIATE` 串行化，只有符合前序的一笔能
  进入新归属，另一笔在第二个确认到达时记为过时。

### 盘点与纠错

`inventory` 声明某地点实见的一组（箱号、代次、封签），与库中该地点应有
状态比对，输出 `missing` / `extra` / `wrong_generation` 三类差异。

`correction` 纠错须：两名不同组的独立签名人、引用一个**具体**的盘点差异、
记录证据与更正内容；且只能引用**最新**一次盘点——禁止把后来的纠错倒填到
旧盘点。纠错绕过正常交接改变记录状态时，箱内介质的自动证据链在该点断开，
纠错本身（证据 + 双签）成为断点处的衔接凭证。

### 证据链（explain）

每次归属/封存变化为介质生成一条证明记录（`proof`，以前序证明 ID 链接）。
`explain` 输出从登记到 `--to` 事务的连续交接证据；若链中断，给出首次断链
的事务、原因与受影响箱代。

### 独立小模型（verify）

`media_custody/model.py` 是与主状态机**零共享代码**的朴素回放实现：按事件
日志重放全部规则，重算每一历史盘点应有的差异，并与库中物化状态、存储的
差异报告、证明链逐一比对。任何不一致（含库被篡改）都以 mismatch 报告。

### 原子性与恢复

批次在单个 SQLite 事务（`BEGIN IMMEDIATE … COMMIT`）内应用：事件日志、
状态表、证明、预约同生同灭。进程中途失败（见测试用独立进程模拟）后重开库，
要么看到完整的旧状态，要么看到完整的新批次，绝不混搭；失败批次不占用
`batch_id`，修正后可直接重发。

## 批次文件格式

```json
{
  "batch_id": "B001",
  "events": [
    {"type": "register_people", "people": [{"id": "P01", "name": "张岚", "group": "G1"}]},
    {"type": "register_media",  "media":  [{"id": "M001", "custodian": "P01",
                                            "witness": "P03", "location": "LAB-A"}]},
    {"type": "box_seal", "box": "BX01", "seal": "SEAL-BX01-1", "media": ["M001"],
     "custodian": "P01", "witness": "P03", "location": "LAB-A"},
    {"type": "box_open", "box": "BX01"},
    {"type": "transfer", "medium": "M001", "new_custodian": "P03",
     "new_witness": "P05", "to_location": "LAB-B"},
    {"type": "handover_confirm", "handover": "H001", "side": "out",
     "box": "BX01", "generation": 1, "media": ["M001"],
     "new_custodian": "P03", "new_witness": "P05", "to_location": "LAB-B"},
    {"type": "correction", "correction": "C001", "snapshot": "SNAP004",
     "box": "BX02", "diff_kind": "wrong_generation",
     "evidence": "现场复核记录", "fix": {"set": {}}, "signers": ["P01", "P04"]}
  ]
}
```

盘点快照文件：`{"snapshot_id": "SNAP001", "location": "LAB-A",
"observed": [{"box": "BX01", "generation": 1, "seal": "SEAL-BX01-1"}]}`。

## 演示场景（data/）

三箱交叉移交：BX01@LAB-A、BX02@LAB-B、BX03@LAB-C 各两件介质，三笔交接的
首个确认乱序到达（B003），第二个确认到齐后交叉移交原子生效（B004）：
BX01→LAB-B、BX02→LAB-C、BX03→LAB-A。SNAP004 中盘点员把 BX02 误记为第 2 代，
产生错代差异；B005 以双签纠错记录现场复核结论；SNAP005 复盘归零。
`verify` 重放全部 17 个事件并对比 5 次历史盘点，结果 `ok`。

## 测试

```bat
python -m unittest discover -s tests -v
```

25 个用例覆盖：乱序确认、清单错一件拒绝整批、开箱与旧确认争用、证据链
中途纠错（含 `--to` 截断）、重复与异内容 ID、盘点前后快照差异、事务故障
后的原子恢复（含独立进程崩溃模拟）、同箱并发批次只成交一笔、同组共管与
封箱单转移的约束拒绝、三箱交叉移交后独立小模型对比全部历史盘点（含篡改
检测）、夹具字节级可重现。

## 目录结构

```
media_custody/     包：db(模式) core(主状态机) model(独立小模型) fixtures cli
tests/             unittest 自动化测试
data/              可重现演示夹具（fixtures 命令生成）
```

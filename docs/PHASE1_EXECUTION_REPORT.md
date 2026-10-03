# Phase 1 执行报告

日期：2026-10-03；执行：ashare-experiment。工作分支 `phase1/data-pipeline`，从 `cb7556a` 建立；
worktree：`E:\量化\ashare-quant-lab\.cache\worktrees\phase1-data`。
主代理拥有 main 集成与服务器主 checkout；本报告批次没有修改这些位置，也没有合并新的 main。

## 第一批：CLI、候选转换和代表样本

实现提交 **b72d41aafb8defdce4d339e985d283390b936469**。
提供明确日期 / API 的计划生成、串行受控采集、精确请求缓存复用、不可覆盖 raw attempt、候选转换、
Parquet / DuckDB 逐项读回和机器可读质量门槛。命令见 [PHASE1_RUNBOOK.md](PHASE1_RUNBOOK.md)，
语义见 [PHASE1_DATA_CONTRACT.md](PHASE1_DATA_CONTRACT.md)。

沿用本地 Python 3.11.16、pandas 2.2.3、PyArrow 19.0.1、DuckDB 1.3.2；
来源 wheel 为 BaoStock 0.9.4，52,424 字节，SHA-256
`0bf71c6069ab5890ff3596632f9c3f8f1fbc6bfcac582c2f9d6a5c11ab2cfaf8`。
来源依赖单独 hash 锁定，不安装 / 升级现有研究环境；uv.lock 未改。

- 全部 **102 项测试通过（1.85 秒）**，Ruff 通过。含 42 项来源管道测试和基线 60 项既有测试。
  初次针对测试发现指数代码误放行、复用 raw 缺 query_id 两项实现错误，已修复后通过；
  最终检查原日志位于 `artifacts/phase1-checks/`，未覆盖 Phase 0 或 Linux/GPU 证据。
- 代表闭环在 clean b72d41a 上执行：**56 个精确请求全部复用，0 个网络查询，5,132 行**。
  八种表的候选行数：成员 4,800、日历 147、行情 104、因子 4、分红 2、基本信息 1、ST 74、停牌名单 0。
- 所有候选 Parquet / DuckDB 逐值读回通过，canonical reader 拒绝读取；转换问题 0、查询内重复 0、
  重叠观测共同字段矛盾 0。零行停牌名单保留，不能据此宣称无停牌。
- `collect` 返回 0；`build` 和官方基准检查返回 **2**，表示研究门槛阻止。这是实际质量结果，
  不是策略表现或研究资格通过。

紧凑证据：[phase1_representative.json](evidence/phase1_representative.json)。
完整 raw run：`datasets/raw/phase1-representative/run.json`；候选 / 质量报告：
`datasets/candidates/phase1-representative/`。原 56 份捕获仍在主仓库 `datasets/raw/baostock`，只读引用并校验摘要。

## 年初成分：已复现的供应商错误

中证 [公告 14497](https://www.csindex.com.cn/#/about/newsDetail?id=14497)发布日期 2022-11-25，
正文第一张沪深 300 表列出 15 只替换，2022-12-09 收市后生效。
原件 JSON SHA-256：`651104e9e2950ad741f41392e0b1cfef1b006c2f41caffb1cc34bd0823b796d6`。
公告附件是 XLSX，初下载扩展名误标 PDF 后已按文件签名及官方名称改为 XLSX；
沪深 300 核对使用正文表，不依赖该附件。

BaoStock **2023-01-03** 返回 300 个唯一代码、updateDate=2022-08-01；
与公告逐代码比较后，**15 只应调入全部缺失，15 只应调出全部仍在**。
这是成员集合本身的错误，不能只标“日期偏旧但 300 只形状通过”。
原始快照不改写为已修复，当前候选仍失败；已核对公告的进出代码可加入后续采集并集，
避免陈旧名单导致连行情采集也遗漏这些股票。

可重放命令：

```powershell
& $phase1Python scripts/verify_csi_baseline.py --announcement datasets/raw/csi/phase1-baseline/announcement-14497.json --events configs/csi-events-2023.json --run datasets/raw/phase1-representative/run.json --output <新的证据文件.json>
```

逐代码结果与 raw 定位：[phase1_baseline_2023.json](evidence/phase1_baseline_2023.json)。
2023 年 6 月公告 14796 的 9 进 9 出仍与既有前后快照完全一致。
两次选定事件不能证明完整年初 300 只基准、全部临时调样或逐日成员时点。

## 仍未通过的事项与后续执行

代表范围未提供全部日历区间和各证券基本信息，因此相关项明确 unverified。
600747 在 outDate 当日缺量且价格沿用仍保留为候选边界问题；完整 ST/*ST 与涨跌停价、
公司行为完整算法及修订历史、历史发布时间和 vintage 均未验收。
税后文本、所有公司行为日期与累计因子字段均保留，未压入 Phase 0 简化 actions。

下一步执行已准备的 59 个日历 / 周度成员 / 调样边界请求（精确旧请求复用），再按已观察和已公告
代码并集采集 2023 年基本信息、未复权日线、分红及因子。该步骤由执行代理继续，不等待新一轮用户催促。
2024–2025 行情、模型、回测、券商、交易和风控均不在本批范围。

第一批采集 / 测试进程均已退出，代表闭环没有网络子进程；本地解释器与所有产物保留。
后续 Phase 1 采集进程仍归执行代理；服务器 Linux 环境 / worktree 及既有产物不动、不重跑。
工具 shell 的 HERDR_ENV 为空，未伪造上下文或从外部操作 Herdr pane；通过本分支报告交付主代理审阅。

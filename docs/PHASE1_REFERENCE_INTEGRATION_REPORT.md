# Phase 1 参考证据接入执行报告

执行代理，2026-10-03。任务依据为主仓库 `docs/PHASE1_REFERENCE_INTEGRATION_PLAN.md`。
独立分支 `phase1/evidence-integration`，基线 `d2a1f90`；主代理负责审阅与集成。
源码、测试、本轮离线构建与产物由执行代理负责。三个旧 worktree 及产物保留。

## 阶段源码交接

本报告同批提交中的源码可独立审阅；两批正式离线重建将在此提交之后从干净源码执行，
运行结果及摘要随后补入本报告。当前没有需主代理决策的实现阻碍。

- `build --evidence-root <main>` 显式启用 `phase1-reference-evidence-v1`。
  先校验 main `d2a1f90` 三份已审摘要的固定 SHA256，再沿保留的计划、metadata、原件、
  提取和 Parquet 校验身份、状态、字节数及内容。旧七探针仍只能使用已核实摘要兼容入口。
- 深交所 XLSX 用标准库 ZIP/XML 重读，逐代码、上市日期、行号及 E/G 单元格对照提取。
  证券范围取原历史计划，当前名称、行业、股本不输出为历史特征。实际观察时间保存，
  历史可用时间及退市日期保持未知。
- 质量 schema 仅在显式证据入口升级为 `public-bars-quality-v2`；候选行情 schema 保持 v1。
  IPO 参考存在、IPO 日期边界、未知退市、未知 PIT、有限 OHLC 比较、未知量单位分别报告。
  上市日前的行情失败；上市当日合法。原参考 IPO 与新观察冲突明确失败，不覆盖原值。
- OHLC 从当前输入行情与已验证腾讯原件重新比较，只选双方请求区间内相同证券/日期。
  输出双方 raw 摘要、行号、原字段、量额单位、字面小数指数、逐字段差值、未覆盖行/字段/证券。
  同源不算独立；重复版本拒绝，多个独立参考冲突全部保留且失败。有限重叠不晋升全年核验。
- 腾讯失败捕获与未执行计划单独保留，不变成成功空数据。候选始终 `research_eligible=false`。
  新构建写 `reference-evidence.json`，manifest 绑定原输入与该文件摘要；不改写旧快照。

## 检查

复用主仓库 `.venv`，未创建环境或改依赖。执行 cwd 为本 worktree，`PYTHONPATH=src`。
测试中的全部新关键边界均为自包含合成 fixture，不需要本地忽略目录，也没有因缺真实原件跳过。

| 检查 | 结果 | 本 worktree 日志 |
| --- | --- | --- |
| 新证据 + 既有 fallback 回归 | 111 passed，7.05 秒 | `artifacts/reference-integration-checks/targeted.txt` |
| 从主仓库 cwd 执行新测试 | 40 passed，1.19 秒 | `artifacts/reference-integration-checks/main-cwd.txt` |
| 短完整测试 | 254 passed，10.25 秒 | `artifacts/reference-integration-checks/full.txt` |
| Ruff / 五个改动 Python 文件格式 | 全部通过 | 同目录 `ruff.txt`、`format.txt` |

主审指出的 `Path.cwd().parents[2]` 测试收集依赖已完全移除。新测试无需发现主仓库；真实原件仅
由显式 `--evidence-root` 入口在正式离线运行中读取。测试覆盖摘要变化、失败/运行中 metadata、
同数量 Parquet 篡改、提取与单元格不符、缺少日期、上市前/上市当日、未知退市、重复/冲突参考、
同源误用、不同日期和区间外行、有限重叠及未覆盖计数，另检查旧 probe CLI 与新旧行情内容一致。

准确 argv、PID、耗时及退出码保存在 `artifacts/reference-integration-checks/execution.json`。
检查 wrapper 曾以系统默认编码解码 Git 中文目录，主仓库 cwd 子进程启动前报 WinError 267；
改为显式 UTF-8 后通过。之前对 worktree 下不存在的 `.venv` 调用也属于本地路径错误。
这些没有网络请求、供应商响应或采集次数，不算数据源失败。

当前 `HERDR_ENV` 未传入工具子进程，因此没有操作 Herdr 或其他 pane。依主代理后续指示，
本分支报告即为阶段交接入口；执行责任继续由本代理承担，主代理可立即开始审阅。

## 待完成的本批运行

接下来从同批稳定源码对已有首十与余九十 raw 各作一次新目录离线构建，验证新旧行情值、
行数与来源摘要完全一致，补入准确 source SHA、输入/产物摘要、退出状态和未核实项。
本批不联网、不重抓、不访问 BaoStock、不动服务器/GPU，不运行模型或回测，不推送 main。

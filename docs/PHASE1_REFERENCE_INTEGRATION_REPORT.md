# Phase 1 参考证据接入执行报告

执行代理，2026-10-03。任务依据为主仓库 `docs/PHASE1_REFERENCE_INTEGRATION_PLAN.md`。
独立分支 `phase1/evidence-integration`，基线 `d2a1f90`；主代理负责审阅与集成。
源码、测试、本轮离线构建与产物由执行代理负责。三个旧 worktree 及产物保留。

## 完成结果与源码

首十和余九十已分别完成新目录离线重建，合计 **100 只、24,200 行**。
两份 `bars.parquet` 均与旧候选逐字节相同；行情全字段、原件身份与摘要未改。
100 只均有经 XLSX 单元格及历史计划验证的 IPO 日期参考，上市前行情 0。
实际独立价格重叠为 **000938.SZ 十行 + 300308.SZ 五行，60 个 OHLC 值，非零差异 0**。
其他 **98 只无独立价格重叠**，全部候选继续 `research_eligible=false`。

源码与实际运行提交为 **`03566ca30972f1fc680f35adab1091c8caf0b797`**。
阶段源码/测试/契约/报告已先提交交主审，随后从干净 worktree 启动两次 CLI；
每份候选 manifest 记录上述 SHA、dirty=false 及相同的 133 份源码文件摘要。
本次最终报告提交不代表重跑，旧批次原始证据也没有追溯改写。

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

准确版本：Python 3.11.16、pandas 2.2.3、PyArrow 19.0.1、DuckDB 1.3.2、
pytest 8.4.1、Ruff 0.12.2。未改依赖、未重复模型/兼容性实验。

## 实际离线构建

运行时间为 2026-10-03 **15:10:22.511663–15:10:31.164082 UTC**（北京时间 23:10）。

| 项目 | 首十 | 余九十 |
| --- | ---: | ---: |
| 行情行数 | 2,420 | 21,780 |
| 已校验 IPO 参考 | 10 | 90 |
| 上市前行情 | 0 | 0 |
| 独立 OHLC 比较行 / 值 | 10 / 40 | 5 / 20 |
| 非零 OHLC 差异 | 0 | 0 |
| 无完整独立 OHLC 比较的行情行 | 2,410 | 21,775 |
| 无独立价格重叠的证券 | 9 | 89 |
| CLI 耗时 | 2.984 秒 | 5.250 秒 |
| CLI 退出码 | 2 | 2 |
| 新候选目录字节数 | 494,996 | 2,347,387 |

退出 2 表示正式研究门禁仍阻止，不是转换或存储失败。两批 Parquet 和 DuckDB 完整往返一致，
正式读取器拒绝候选。转换、重复、排序、请求边界、闭市日及日历缺行检查均通过。

000938 的独立日期为 2023-12-18 至 12-29 十个开市日；300308 为 2023-07-18 至 07-24 五个
开市日。各批显式列出 OHLC 未覆盖的股票、行和字段。腾讯这两只量单位仍未知，不做量比较；
显示金额绝对差最大值分别为 43.13 元、45.00 元，原始值、单位及字面小数指数均保留。
有限 OHLC 相同不能验证全年，更不能验证可成交性或 PIT。

证据导入读取 34 份绑定文件，逐项校验；重读 XLSX 后为原历史计划内的 134 个代码保留上市日期
参考，本批 100 个候选代码全部有参考，最晚上市日期为 2022-07-29。当前官方名单的观察时间
保留为 2026-10-03，历史可用时间、退市日及语义仍空。腾讯五个成功 raw 保留 50 行，包括
区间外行；新交叉批次原先的 10 计划 / 2 尝试 / 1 成功 / 1 TLS 失败 / 8 未尝试仍如实记录。
原 BaoStock 价格对照在这两批仍为 0 重叠，旧 `cross_source_differences` 保持 unverified；
新增 `cross_source_ohlc_overlap` 单独报告腾讯有限交集，避免把两项零差异混为已通过。

## 命令与产物

执行 cwd：`E:\量化\ashare-quant-lab\.cache\worktrees\phase1-evidence-integration`。
环境 `PYTHONPATH=src`、`PYTHONIOENCODING=utf-8`。下列 `<MAIN>` 为
`E:\量化\ashare-quant-lab`，`<OLD>` 为其 `.cache/worktrees/phase1-data`，`<FALLBACK>` 为其
`.cache/worktrees/phase1-source-fallback`；准确绝对 argv/PID/时点在紧凑证据中。

```text
<MAIN>/.venv/Scripts/python.exe -m ashare_lab.fallback.cli build --run <FALLBACK>/datasets/raw/eastmoney/20261003-missing-first10/run.json --reference <OLD>/datasets/candidates/phase1-2023-provenance-verified --evidence-root <MAIN> --output datasets/candidates/eastmoney-2023-first10-evidence
<MAIN>/.venv/Scripts/python.exe -m ashare_lab.fallback.cli build --run <FALLBACK>/datasets/raw/eastmoney/20261003-missing-remaining90/run.json --reference <OLD>/datasets/candidates/phase1-2023-provenance-verified --evidence-root <MAIN> --output datasets/candidates/eastmoney-2023-remaining90-evidence
```

| 产物 | SHA256 |
| --- | --- |
| 首十 `bars.parquet`（与旧文件相同） | `01650c4c28ca9bd8720ef590fe77d7a5866729d472b226f23a33b6e065a093d6` |
| 余九十 `bars.parquet`（与旧文件相同） | `8db14e4281f93f3ce7bed89d730cc755e2993f938d490ca4b7bc48bd81679004` |
| 首十新 manifest | `b0ac04e31b3d4ed1063438f676005b6d75c7729987ec85463ae1badf161afe2c` |
| 余九十新 manifest | `9598b045ce817e4d9f040173381a101488e5f682706cb429c8406274163fa3cf` |
| 两批 `reference-evidence.json` | `f6c8acec17933b3e262b0a9b3591d96fd1a697258e80464dbe260559d77fc7bc` |

两批共新增 2,842,383 字节候选目录。每目录含行情、转换问题、质量报告、参考证据及 manifest。
完整运行/检查日志和旧文件摘要快照在 `artifacts/reference-integration*`；执行及证据脚本在
`.cache/phase1/reference_execute.py`、`reference_evidence.py`。紧凑交接索引：
[phase1_reference_integration_execution.json](evidence/phase1_reference_integration_execution.json)，
包含来源/计划/原件/输出/日志/脚本摘要、版本及准确命令，不将大型原件纳入 Git。

## source-state、进程与未完成验收

470 份受保护文件的运行前后字节数与 SHA256 全部相同，覆盖原行情/候选、官方原件与摘要、
计划、BaoStock run、来源账本与限制文件。BaoStock 账本仍为 **1998**，错误 **10001011** 及
`automatic_retry=false` 保留；原 **401** 未完成请求不被补充来源抵扣。东方财富账本仍为 **100**。

本轮 wrapper PID **59496** 正常退出 0；两次 CLI PID **51996 / 43684** 已等待退出 2。
完成后的单次有界进程核验三者均不在进程列表，没有保留本轮后台任务。源码分支、新产物与
原始证据继续由执行代理保留；主代理接手审阅和 main 集成，无进程需要主代理接管。
当前工具进程未继承 `HERDR_ENV`，未操作 Herdr 或其他 pane；依主代理指示通过本分支报告交接。

本批范围已完成，以下研究验收仍未完成：历史成分资格、退市语义、历史可用时间及修订、全年
独立行情对照、未知量单位及舍入、ST/停牌/涨跌停/交易前收、复权与公司行为。候选和门禁未晋升。
没有联网、重抓、修改来源状态、服务器/GPU、模型/回测、2024–2025 行情或 main 推送操作。

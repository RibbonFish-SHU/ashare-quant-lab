# Phase 1 补充行情执行报告

执行代理，2026-10-03。分支 `phase1/source-fallback`，工作区
`E:\量化\ashare-quant-lab\.cache\worktrees\phase1-source-fallback`。
主代理拥有任务书、主审、main 集成及后续采集范围决策。

## 交付与源码归属

1. 实现及实际离线 / 联网运行提交：`2bb40e42bc1fc7c506d6c2389cbbb98d3a13340d`。
   新增 `ashare_lab.fallback` 的 plan / probe / collect / build CLI、独立候选 schema、
   来源完整性验证、质量检查和测试。运行工作区干净，两份候选 manifest 各记录 76 个源码摘要。
2. 实采后发现的节流边界修复：`3b5c9fe636c6df16f65f8450551d744059d2973d`。
   仅改变未来采集的等待基准，新增离线回归；没有重复真实请求或重写运行记录。
3. 契约说明：[PHASE1_FALLBACK_CONTRACT.md](PHASE1_FALLBACK_CONTRACT.md)。
   紧凑执行证据：[phase1_fallback_execution.json](evidence/phase1_fallback_execution.json)，
   含命令、版本、日志和产物摘要、逐请求记录、进程及实际时间间隔。

环境复用项目 `.venv`：Python 3.11.16、pandas 2.2.3、PyArrow 19.0.1、DuckDB 1.3.2、
pytest 8.4.1、Ruff 0.12.2；平台标识 `Windows-10-10.0.26200-SP0`。未增加依赖或建立新环境。

## 预审问题与验证

保留 `artifacts/fallback-checks/prereview-before.txt` 的 **6 个先验失败**。
新 raw 强制 `classification=complete`、`transfer_complete=true`、无 error / exception，
并核对 HTTP、字节数、摘要、Content-Length、URL / 参数、供应商整数成功码及证券身份。
entry.complete 无法晋升失败、运行中、部分或矛盾 raw；采集恢复也使用相同验证。
七份旧 probe 的例外只对固定 metadata / body 摘要组合开放，原件未改。

上市日期未知、无独立重叠或只有部分重叠时均报告 unverified，不能以零差异冒充核对通过。
另覆盖成功空数组、非成功空对象、HTML/challenge、重复 / 乱序 / 越界、显示精度、数值缩放溢出、
截断响应、有限重试、停止限制和恢复复用。测试全部替换网络，不调用在线服务。

| 检查 | 实际结果 | 日志（工作区相对路径） |
| --- | --- | --- |
| 实采前针对回归 | 47 passed，0.49 秒 | `artifacts/fallback-checks/targeted-final.txt` |
| 实采前完整测试 | 185 passed，3.72 秒 | `artifacts/fallback-checks/full.txt` |
| 实采前 Ruff | 通过 | `artifacts/fallback-checks/ruff.txt` |
| 节流问题先验回归 | 1 failed，47 deselected | `artifacts/fallback-checks/timing-before.txt` |
| 节流及恢复相关回归 | 3 passed，45 deselected，0.32 秒 | `artifacts/fallback-checks/timing-after.txt` |
| 节流修复后完整测试 | 186 passed，4.10 秒 | `artifacts/fallback-checks/full-after-timing.txt` |
| 节流修复后 Ruff | 通过，7 个文件格式检查通过 | `artifacts/fallback-checks/ruff-after-timing.txt` |

主代理已在当前对话确认 `2bb40e4` 的来源完整性源码主审通过；其验收报告由主代理独立维护。

## 七份原件离线重放

复用主仓库 `datasets/raw/{eastmoney,tencent}/20261003-phase1-probe/`，没有新联网。
参考为旧数据 worktree 的 `datasets/candidates/phase1-2023-provenance-verified/`。
输出 `datasets/candidates/fallback-seven-probes/`。

| 范围 | 实际结果 |
| --- | --- |
| 原件 / 原始行 / 区间内行 | 7 / 54 / 33 |
| 独立重叠 / OHLC 比较 | 28 行 / 112 值，OHLC 差异 0 |
| 原始区间外行 | 腾讯 21 行向前溢出，保留但不计覆盖或比较 |
| 显示量差异 | 最大 44 股；不标为精确成交股数 |
| 显示金额差异 | 东方财富最大 0.40 元；腾讯最大 49.51 元 |
| 全字段非零差异 | 47 项量额差异，全部保留定位 |
| 参考日历缺行 | 688065 的 06-16、06-19、06-20；两个来源共 6 个请求 / 日期缺口 |
| 未核实 | 四只股票上市边界；300308 的 5 个区间内行无独立对照，10 个 raw 行的腾讯量单位未知 |

Parquet 和 DuckDB 完整读回均通过；正式数据读取器拒绝候选。报告状态 unverified，
`research_eligible=false`。CLI 退出 2 表示研究门禁阻止，不是读取失败。

## 首十只东方财富实采

以原 BaoStock 证券 run 和保留原计划重新核实 **100 个缺失日线请求**，按代码字典序取前十个。
计划摘要 `ff10586e24213fa69f1744704ab62ff95205579851c6ee60c2b73cb9ddb102b9`；
独立原计划副本摘要相同。原 BaoStock 全部 **401 个未完成请求**及其历史含义不变。

所选证券：000938、000963、000977、000983、000999、001289、001979、002001、002007、002008，
均为 SZ。全部参数显式限定 `20230101–20231231`、`klt=101`、`fqt=0`，
只访问已核对的东方财富固定日线端点。

2026-10-03 **12:44:12.919522–12:44:24.065184 UTC**（北京时间 20:44），
CLI 用时 **11.145565 秒**：10 次尝试 / 10 只证券 / 10 次 HTTP 200，全部完整，
0 次重试、0 次失败、0 个未完成请求，响应正文合计 **189,653 字节**。
没有遇到限制 / challenge，未创建东方财富限制文件。

输出：

- raw：`datasets/raw/eastmoney/20261003-missing-first10/`，含每次请求、完整 body、metadata、
  worker 日志、独立原计划及 run；共 242,716 字节。
- 候选：`datasets/candidates/eastmoney-2023-first10/`，共 257,006 字节。
- 独立锁 / 账本：主仓库 `.cache/phase1/eastmoney-source-access/`，共 3,629 字节。

**共 2,420 行，每只 242 行**，首日 2023-01-03、末日 2023-12-29，与已保留 2023 日历全部
开市日期吻合。数值转换、重复、乱序、越界、闭市日、参考日历覆盖和日历缺行检查均无问题；
两种存储完整读回通过，正式数据读取器拒绝候选。

以下仍是未核实：十只证券均没有可验证 IPO / outDate 参考；**跨源比较实际为 0 行**，
因此 `cross_source_differences=0` 的状态为 unverified。新的数量只是独立东方财富候选覆盖，
不能据此将缺失 BaoStock 数据补成同一来源，更不能提升 PIT 或执行资格。

### 实际节流偏差及修复

实采使用 `2bb40e4`：父进程 attempt 启动间隔最短 **1.000636 秒**，但 worker 的捕获开始
时间间隔最短 **0.989708 秒**，另有一处 0.995887 秒。子进程导入耗时变化使父进程的
一秒间隔不能保证实际请求开始相隔一秒。未记录底层 socket 发包时间，
**本批不宣称严格的一秒线上请求间隔已经验收**。

发现后立即向主代理披露，保留 10 次原始时间和账本。`3b5c9fe` 将节流改为上一 worker
退出后再等待至少一秒，并把退出时点写入独立账本供恢复使用。先失败后通过的离线测试
模拟不同耗时的 worker，复用 / 重试回归也通过。该修复没有新增联网验证；不为重验重复已取得的行情。

## 实际命令与产物

以下在当前 worktree 执行，`PYTHONPATH=src`、`PYTHONIOENCODING=utf-8`。
`<PY>` 为 `E:\量化\ashare-quant-lab\.venv\Scripts\python.exe`，
`<MAIN>` 为主仓库，`<OLD>` 为 `.cache/worktrees/phase1-data` 的绝对路径：

```text
<PY> -m ashare_lab.fallback.cli probe --source-root <MAIN> --reference <OLD>/datasets/candidates/phase1-2023-provenance-verified --output datasets/candidates/fallback-seven-probes
<PY> -m ashare_lab.fallback.cli plan --baostock-run <OLD>/datasets/raw/phase1-securities-2023/run.json --original-plan <OLD>/.cache/phase1/plans/securities-2023.json --output .cache/phase1/plans/eastmoney-missing-2023-first10.json --limit 10
<PY> -m ashare_lab.fallback.cli collect --plan .cache/phase1/plans/eastmoney-missing-2023-first10.json --output datasets/raw/eastmoney/20261003-missing-first10
<PY> -m ashare_lab.fallback.cli build --run datasets/raw/eastmoney/20261003-missing-first10/run.json --reference <OLD>/datasets/candidates/phase1-2023-provenance-verified --output datasets/candidates/eastmoney-2023-first10
```

四条命令退出码依次为 **2 / 0 / 0 / 2**。实际执行参数使用绝对路径，完整命令、开始 / 结束
时点、CLI / wrapper PID、日志摘要见紧凑证据及 `artifacts/fallback-batch/execution.json`。
任务本地记录脚本保留在 `.cache/phase1/fallback_execute.py` 与 `fallback_evidence.py`，
不属于研究运行库。日志不覆盖，所有原始成功及先验失败证据保留。

## 资源、进程和未完成验收

本批 raw、两份候选和独立状态目录合计 **594,378 字节**，未清理任何原件或他人文件。
E 盘可用字节在记录起止分别为 **1,509,804,470,272 / 1,509,803,503,616**；
这些是整盘瞬时值，不把差额冒充本任务峰值。未监测磁盘 / 内存峰值，无大型下载。

采集 CLI PID 42192；十个 worker PID 为 21780、73664、74872、73500、11892、68532、
60264、48720、54588、16864，全部正常退出 0。2026-10-03 12:45:56 UTC 对记录的
18 个 wrapper / CLI / worker PID 做一次有界核验，均已退出，无常驻或等待中的采集进程。
`artifacts/fallback-batch/process-exit.json` 保留检查结果。

BaoStock 的 `source-restriction.json`、`wire-ledger.json`、原证券 run 和原计划四份文件，
前后字节数 / SHA256 完全一致；七份 probe 的 metadata / body 和输出摘要也再次核实。
没有访问 BaoStock，没有腾讯新请求，没有 2024–2025 行情请求，没有模型 / 回测 / GPU，
没有服务器操作、main 修改或远端推送。

仍需主代理按数据质量决定：余下 **90 个**原计划缺失日线是否扩大东方财富采集，
以及新候选如何获得独立抽样对照和上市信息。历史成分、ST / 停牌 / 涨跌停、公司行为、
发布时间和修订版本、执行价格 / 前收及显示量额精度均未完成研究验收。
这十只即使日历覆盖完整也全部保持 `research_eligible=false`。

本分支源码、采集与产物由执行代理负责并保留；主代理接手审阅、集成和下一范围决策。
当前工具环境 `HERDR_ENV` 为空，未调用 Herdr 外部控制、未伪造上下文；通过当前对话及本报告交接，
不向其他 pane 发送消息。旧 worktree、环境及全部有效证据保持完整。

## 补充传输边界复核

收到新的传输复现交接后，在 `6acd140` 上离线核对主代理保留的
`fallback-short-body-d_o1unac` 和 `fallback-provenance-017ifohr`。
两份旧复现记录的源码摘要与当前源文件不同；Content-Length 和失败 metadata 修复已在
`2bb40e4`，本次没有修改生产源码、测试或重新采集。

以已保存的真实 573 字节 / 5 行 body 模拟 HTTP：声明 583 字节时返回
`incomplete_transfer`、`transfer_complete=false`，原始 body 保留，候选读取拒绝；
声明恰好 573 字节以及无 Content-Length 的正常 EOF 两种完整响应均返回 complete，
读取 5 行。模拟连接重置返回可重试的 `transport_error`，失败 metadata 仍无法晋升。
原先接受 5 行的失败元数据 fixture 现按 `HTTP raw is not a complete successful response` 拒绝。

13 项相关回归通过（35 deselected，0.29 秒），涵盖有限重试、challenge 停止、失败清单、
旧格式例外及传输长度；沿用未变化源码的 186 项完整测试和 Ruff 证据。
既有保守策略不变：`transport_error` / `worker_timeout` 最多两次尝试；
`incomplete_transfer` 拒绝并终止，不在此次复核中自动扩大重试范围。

source-state 证据见 [phase1_fallback_transfer_recheck.json](evidence/phase1_fallback_transfer_recheck.json)。
10 份受保护 fixture / 原件 / 原 run / 两个来源账本前后摘要相同；联网请求 0，
生产来源状态文件未写入，无待运行采集进程。离线输出保留在
`artifacts/fallback-transfer-recheck/`，任务脚本位于 `.cache/phase1/recheck_transfer.py`。

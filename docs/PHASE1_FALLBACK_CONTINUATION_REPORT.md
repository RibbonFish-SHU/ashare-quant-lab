# 2023 东方财富剩余 90 只续采执行报告

执行代理，2026-10-03。任务依据为主仓库 `docs/PHASE1_FALLBACK_CONTINUATION_PLAN.md`；
主代理负责总体方案、审阅和集成，执行代理负责本分支源码、测试、采集、产物及报告。
工作区为 `E:\量化\ashare-quant-lab\.cache\worktrees\phase1-source-fallback`，
分支 `phase1/source-fallback`。

## 结果与源码

**90/90 个续采请求成功，21,780 行；每只 242 行，日历缺行 0。**
结合首十只，东方财富针对原 100 个缺失日线的候选覆盖为 **100/100、24,200 行**。
该计数只描述独立来源的原始获取，BaoStock 的 **401 个未完成请求及 1998 请求账本不变**。
候选全部 `research_eligible=false`，没有进入模型或回测。

本轮源码与全部实际 CLI 运行提交：`c11fca6f46a99a35cd30246539fbd8674ef67aa8`。
源码提交后从干净 worktree 执行，候选 manifest 记录该 SHA 和 80 个源码文件摘要。
首批报告已先提交 `6acd140`，补充离线复核为 `3001e51`；原数据及原运行证据保留，
不把报告更新或后续源码冒充重新执行。

新入口 `plan-continuation` 生成 `fallback-continuation-plan-v1`，新 run 为
`public-http-run-v2`。旧 pilot schema 和最多十只门禁保持有效。
新计划绑定原 BaoStock run / 原计划及已审首批 run / 原计划四份摘要，核对十份成功权威 raw 后
才排除首批；要求同一原始缺失 100 集合、完整十次成功，固定余下 90 只代码字典序。
修改嵌入计划、分母、排除集合、身份、状态、年份或来源均无法绕过验证。

范围从 `002027.SZ` 到 `301269.SZ`，完整 90 代码在执行证据和原计划中。全部请求均限定
2023-01-01 至 2023-12-31、`klt=101`、`fqt=0`，仍使用首批东方财富端点。
共享来源锁、worker 退出后等待至少一秒、超时 / 重试 / 停止策略沿用已审实现。

## 测试与环境

复用现有 `.venv`：Python 3.11.16、pandas 2.2.3、PyArrow 19.0.1、DuckDB 1.3.2、
pytest 8.4.1、Ruff 0.12.2。未改依赖或建立新环境。

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| 续采及原 fallback 回归 | 71 passed，7.41 秒 | `artifacts/fallback-continuation-checks/targeted.txt` |
| 短完整测试 | 209 passed，9.65 秒 | `artifacts/fallback-continuation-checks/full.txt` |
| Ruff / 相关格式 | 通过，8 个文件格式通过 | `artifacts/fallback-continuation-checks/ruff.txt` |

新回归使用明确的合成离线归档，验证：排除集合固定、四份绑定文件字节变化、计划删减 / 重排、
来源和年份 / 复权越界、pilot 未完成 / 失败 / worker 失败、有效 body 搭配失败 metadata、
证券身份变化、pilot schema 替换、修改独立原计划、其他原始计划混入，以及恢复时复用已有成功
capture。恢复测试完成剩余逻辑请求后再次执行不会发出模拟请求；不使用在线服务测试。
本批只改计划和报告入口，未重复已有模型、Linux 或 GPU 验证。

## 实际采集与质量

采集时间：**2026-10-03 13:10:25.911490–13:12:43.843043 UTC**，北京时间 21:10–21:12，
CLI 实际用时 **137.930828 秒**。90 个逻辑请求 / 90 次传输尝试，全部 HTTP 200，
90 次 complete、0 次重试、0 次失败、0 次空返回、0 个未完成请求。
完整响应正文合计 **1,720,763 字节**。未遇到 provider / protocol / challenge / 短响应错误，
未写入东方财富限制文件。

实测相邻 worker 捕获开始最短间隔 **1.384070 秒**；前一个 worker 退出到下一个父进程
attempt 开始最短间隔 **1.000194 秒**，89 个间隔全部至少一秒。
原始时点保存于 metadata 和共享账本，紧凑证据保留间隔数组。这里使用程序记录的捕获 / 退出
时点，没有声称记录了底层 socket 发包时点。首批原有 0.989708 秒偏差仍留在首批报告。

全部证券覆盖 2023-01-03 至 2023-12-29 的 242 个参考开市日：数值转换、重复、排序、越界、
闭市日、参考日历覆盖及缺行检查均通过。Parquet 和 DuckDB 完整读回一致，正式数据读取器
拒绝候选；build 退出 2 表示研究门禁阻止。

以下保持 unverified：

- 90 只证券均缺少已核实的上市 / 退市参考日期和相应语义，不能写上市边界通过。
- 主质量报告对照固定 BaoStock 候选，实际重叠为 0 行；零差异不代表已核对。
- 显示成交量虽按手乘 100，仍不是精确成交股数；量额实际精度和舍入规则未证实。
- ST、停牌区间、涨跌停、交易前收、发布时间、历史修订及历史成分资格未验收。

另复用已保留腾讯 300308 的 2023-07-18、19、20、21、24 五行做独立离线抽样。
**20 个 OHLC 值全部一致**；按各来源显示单位换算的金额最大差 **45 元**。
腾讯该证券成交量单位仍未知，不进行强制量比较。这五行仅是有限对照，不覆盖其他行或赋予
研究资格。双方 raw 摘要和行号、逐字段差值在
`artifacts/fallback-continuation/retained-tencent-comparison.json`；没有新腾讯请求。

## 命令与不可覆盖产物

执行目录为本 worktree，环境 `PYTHONPATH=src`、`PYTHONIOENCODING=utf-8`。
`<PY>` 为主仓库 `.venv/Scripts/python.exe`，`<OLD>` 为旧 `phase1-data` worktree 的绝对路径。
实际命令使用绝对路径，其 argv、PID、日志和时点完整保存在执行证据。

```text
<PY> -m ashare_lab.fallback.cli plan-continuation --baostock-run <OLD>/datasets/raw/phase1-securities-2023/run.json --original-plan <OLD>/.cache/phase1/plans/securities-2023.json --pilot-run datasets/raw/eastmoney/20261003-missing-first10/run.json --pilot-plan .cache/phase1/plans/eastmoney-missing-2023-first10.json --output .cache/phase1/plans/eastmoney-missing-2023-remaining90.json
<PY> -m ashare_lab.fallback.cli collect --plan .cache/phase1/plans/eastmoney-missing-2023-remaining90.json --output datasets/raw/eastmoney/20261003-missing-remaining90
<PY> -m ashare_lab.fallback.cli build --run datasets/raw/eastmoney/20261003-missing-remaining90/run.json --reference <OLD>/datasets/candidates/phase1-2023-provenance-verified --output datasets/candidates/eastmoney-2023-remaining90
```

退出码依次 **0 / 0 / 2**。核心产物（本 worktree 相对路径）：

| 产物 | 路径 / SHA256 |
| --- | --- |
| 可验证原计划 | `.cache/phase1/plans/eastmoney-missing-2023-remaining90.json`；`fd0a618e0ae46105bc099f52e57933c9fe2feeb99117d9cb41300e40e43c907c` |
| 独立 raw run | `datasets/raw/eastmoney/20261003-missing-remaining90/run.json`；`b66a3c11187065cc7aca5f3a2c9c2c6b25915e5666048e33d37c4898c0bc524d` |
| 独立候选 manifest | `datasets/candidates/eastmoney-2023-remaining90/manifest.json`；`ea70ff5e99066f9560fe8fa95a68d600ccab0edf944e02644c8e19cbcf57d903` |
| 运行日志、账本快照、进程证据 | `artifacts/fallback-continuation/` |
| 紧凑交接证据 | [phase1_fallback_continuation_execution.json](evidence/phase1_fallback_continuation_execution.json) |

raw 内保存每次 request、完整 body、metadata、worker 日志和独立 `plan.original.json`，
计划副本与来源计划摘要相同。候选包含 `bars.parquet`、质量报告和转换问题表，所有摘要已核对。
任务专用记录 / 证据脚本保留于 `.cache/phase1/continuation_execute.py`、`continuation_evidence.py`。

## source-state、资源与进程交接

首批 raw / 候选、原计划、BaoStock run / 原计划 / 限制 / 账本共 **55 份受保护文件**，
前后字节数和摘要完全一致。东方财富共享账本按本批授权新增 90 条，累计 100 条，原十条前缀逐项相同；
运行前后账本的独立副本均保留在本批 artifacts，避免后续合法追加影响旧证据追溯。
BaoStock 仍为 1998 次，限制未清除，历史 401 未完成不被跨来源覆盖抵扣。

本批 raw 目录 **2,085,217 字节**，候选目录 **2,029,402 字节**，合计 **4,114,619 字节**。
E 盘可用字节起止为 **1,509,801,357,312 / 1,509,795,684,352**，整盘差额不当作任务峰值；
未测量内存 / 磁盘峰值。没有大型环境、清理或他人文件操作。

采集 wrapper PID 21820、CLI PID 56552；90 个 worker 全部正常退出 0。
2026-10-03 13:15:29 UTC 对本轮记录的 **96 个 wrapper / CLI / worker PID** 作一次有界核验，
全部已退出，未保留后台采集进程。精确 PID 清单保留在执行证据和 `process-exit.json`。
所有本批原件及产物由执行代理保留；主代理接手审阅、main 集成和下一数据质量任务决策。

本地诊断曾有中文路径读取缺少 UTF-8、`rg` 文件名 / 通配符错误，已明确记入首批报告。
这些是本地工具错误，不是供应商响应，不计入本批 90 次实际网络结果。
旧 Content-Length 排队意见已关闭，本轮没有为它重复采集或再建修复批次。

没有访问 BaoStock 或其他新来源，没有 2024–2025 行情请求，没有模型 / 回测 / GPU / 服务器
操作或远端推送。剩余工作是上述数据资格与语义缺口；本轮原 100 缺失日线的补充获取范围已完成。

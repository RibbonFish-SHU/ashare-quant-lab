# ashare-quant-lab

A 股日频研究工程。已验证本地 / Linux 环境、数据契约、CPU / 单卡 GPU smoke 和真实原始样本读取；
Phase 1 数据管线已集成，2023 候选共 75,271 行，1,403 个计划请求中成功 1,002 个。
BaoStock 访问受限后已停止该来源；东方财富补充 100 只的 24,200 行日线已完成原件与落盘核对。
两个来源分别保留，原 336 代码计划均已有日线候选；历史 PIT、回测和正式策略尚未验收，
真实候选均不具有正式研究资格。
另已下载 Qlib 推荐的免费社区历史包，完成原计划 336 只的 2023 价格核对及六只原生 provider 抽样，
并核实包内历史成分的时间偏差；见[公开数据包实测](docs/PHASE1_OPEN_DATA_REVIEW.md)。
100 只补充证券的官方上市日期及有限腾讯交叉证据已接入可重放质量报告，旧行情文件保持相同。
最新已用公告解释 17 个停牌缺价日，并生成独立条件成员日历：33 个原范围缺价日中，
当日条件成员内有 4 个，均对应已核实停牌；社区调样滞后的 27 个交易日有逐日诊断。
条件成员的基准与临时调整完整性仍未认证，见[缺口解决与运行证据](docs/PHASE1_GAP_RESOLUTION_REPORT.md)。

另有独立的[合成执行账本](docs/EXECUTION_CONTRACT.md)，验证次交易日订单、T+1、交易单位、
容量、费用和未成交持仓延续。它尚未接入真实候选，输出不代表等权策略回测结果。
[主审报告与公司行为补证清单](docs/PHASE1_EXECUTION_READINESS.md)记录本批 532 项测试、干净提交 smoke 和剩余数据缺口。
后续已接入[合成公司行为账本](docs/CORPORATE_ACTION_CONTRACT.md)，将登记权益、除息应收、现金支付、
送转股到账和可卖日分开处理；181 项相关测试及独立产物核对通过，见[公司行为主审](docs/PHASE1_CORPORATE_ACTION_REVIEW.md)。
后续[合成估值层](docs/VALUATION_CONTRACT.md)已计入现金应收和待到账股份，253 项相关测试与
六阶段资产核对通过，见[估值主审](docs/PHASE1_VALUATION_REVIEW.md)。真实税制、配股与真实收益核算仍待补齐。

官方分红补证已完成 95 只关键词查询及 14 只类别补查，保留 119 个不同公告 ID；
7 份所选原件形成独立审阅候选，另核对了 TCL 中环调整分派日期的三份公告。
数值差异、预案和未知可用时间保留，详见[分红原件审阅](docs/PHASE1_DIVIDEND_EVIDENCE_REVIEW.md)。
后续又完成 10 次查询、8 份原件核验，为六只标题缺口证券连接年度不分配或年报批准证据；
不同批准范围与原文年份冲突单独保留，见[分红缺口补证](docs/PHASE1_DIVIDEND_GAP_FOLLOWUP.md)。
成员公告后续新增 50 份完整正文及 6 份附件核验，累计 64 条索引记录已有选择性审阅；
接口限制与仍缺的历史覆盖见[成员公告补审](docs/PHASE1_MEMBERSHIP_DETAIL_FOLLOWUP.md)。
新增独立公告时间诊断，分别查询日期假设与实际捕获事实；83 项专测和 17 个真实原件案例通过。
三份复牌公告的同日开盘缺口及晚附件影响见[公告时间主审](docs/PHASE1_PUBLICATION_TIMING_REVIEW.md)，
诊断不会解除真实数据的研究门禁。
交易状态已按原范围及条件成员日分别盘点；95 只条件成员的 21,050 个证券日仍缺来源状态标记，
完整排序和已解释停牌日见[逐日状态缺口](docs/PHASE1_TRADING_STATE_COVERAGE.md)。

```powershell
# Windows，已安装 uv 时：使用本项目独立的 Python 3.11.16 和 .venv
uv python install 3.11.16 --install-dir .cache/phase0/python --no-bin
uv sync --frozen --extra dev --python .cache/phase0/python/cpython-3.11.16-windows-x86_64-none/python.exe
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev ruff check src tests
uv run --frozen --extra dev ashare-lab smoke --config configs/smoke.toml
```

每次 smoke 在 `artifacts/experiments/synthetic/` 下生成独立目录，保存配置、源码提交与工作区摘要、
完整包版本、日志、数据清单和成功 / 失败状态。默认 CPU，不连接行情服务或券商。

- [原始方案](PROJECT_PLAN.md)与[当前任务书](docs/PHASE0_PLAN.md)
- [数据契约与标签时序](docs/DATA_CONTRACT.md)
- [安装、执行、故障和资源说明](docs/RUNBOOK.md)
- [本轮执行报告与验收缺口](docs/PHASE0_EXECUTION_REPORT.md)
- [主代理审阅结论与修复验收](docs/PHASE0_REVIEW.md)
- [Linux 环境、容量及单卡 GPU 实测](docs/PHASE0_LINUX_REPORT.md)
- [数据源决策与未冻结时间划分](docs/DATA_SOURCE_DECISION.md)
- [免费来源实测与真实原始数据读取](docs/PHASE0_DATA_PROBE_REPORT.md)
- [Phase 1 采集、适配与质量审核任务](docs/PHASE1_PLAN.md)
- [Phase 1 运行入口与数据契约](docs/PHASE1_RUNBOOK.md)
- [Phase 1 执行报告](docs/PHASE1_EXECUTION_REPORT.md)与[主审验收](docs/PHASE1_REVIEW.md)
- [补充行情来源实测与下一批范围](docs/PHASE1_SOURCE_FALLBACK_PLAN.md)
- [补充行情首批报告](docs/PHASE1_FALLBACK_REPORT.md)、[续采报告](docs/PHASE1_FALLBACK_CONTINUATION_REPORT.md)与[主审结果](docs/PHASE1_FALLBACK_REVIEW.md)
- [上市日期与独立价格抽样](docs/PHASE1_METADATA_AND_CROSSCHECK.md)
- [免费社区历史数据包及成员口径实测](docs/PHASE1_OPEN_DATA_REVIEW.md)
- [参考证据接入主审](docs/PHASE1_REFERENCE_INTEGRATION_REVIEW.md)与[执行记录](docs/PHASE1_REFERENCE_INTEGRATION_REPORT.md)
- [停复牌、公司行为原件与条件成员缺口](docs/PHASE1_GAP_RESOLUTION_REPORT.md)

研究、推理、交易和风控保持隔离。当前代码仅有研究工程工具；实盘晋级依照项目方案另行确认。

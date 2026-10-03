# ashare-quant-lab

A 股日频研究工程。已验证本地 / Linux 环境、数据契约、CPU / 单卡 GPU smoke 和真实原始样本读取；
Phase 1 数据管线已集成，2023 候选共 75,271 行，1,403 个计划请求中成功 1,002 个。
BaoStock 访问受限后已停止该来源；东方财富补充 100 只的 24,200 行日线已完成原件与落盘核对。
两个来源分别保留，原 336 代码计划均已有日线候选；历史 PIT、回测和正式策略尚未验收，
真实候选均不具有正式研究资格。

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

研究、推理、交易和风控保持隔离。当前代码仅有研究工程工具；实盘晋级依照项目方案另行确认。

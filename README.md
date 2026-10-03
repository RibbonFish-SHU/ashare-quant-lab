# ashare-quant-lab

A 股日频研究工程。当前为 Phase 0：供应商无关的数据契约、时间边界、可复现运行入口和依赖 smoke。
合成样例只验证工程行为，尚未验收真实数据、回测或正式策略。

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
- [数据源决策与未冻结时间划分](docs/DATA_SOURCE_DECISION.md)

研究、推理、交易和风控保持隔离。当前代码仅有研究工程工具；实盘晋级依照项目方案另行确认。

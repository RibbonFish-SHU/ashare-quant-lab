# Phase 1 本地采集与候选质量检查

使用已验收的 Python 3.11.16 研究环境，工作目录为独立 Phase 1 worktree。
主仓库的 raw 捕获可只读复用。以下路径是本轮实际布局；新输出目录必须不存在。
2026-10-03 本次采集已收到 BaoStock `10001011` 黑名单错误；共享限制状态和账本保留。
目前仅执行离线重放，不能据这些历史命令自动恢复网络采集。恢复条件由主代理协调确认。

```powershell
$phase1Python = 'E:\量化\ashare-quant-lab\.venv\Scripts\python.exe'
$phase1Raw = 'E:\量化\ashare-quant-lab\datasets\raw\baostock'
$phase1Wheel = 'E:\量化\ashare-quant-lab\.cache\phase0\data-probe\baostock-0.9.4-py3-none-any.whl'
$env:PYTHONPATH = 'src'
$env:PYTHONIOENCODING = 'utf-8'
& $phase1Python -m ashare_lab.data.cli collect --plan configs/phase1-representative.json --output datasets/raw/phase1-representative --reuse-root $phase1Raw --offline
& $phase1Python -m ashare_lab.data.cli build --run datasets/raw/phase1-representative/run.json --original-plan configs/phase1-representative.json --events configs/csi-events-2023.json --output datasets/candidates/phase1-representative
```

代表计划由 `plan-reuse --raw-root <root> --output <new.json>` 生成，精确复用 56 个原始成功请求。
`build` 返回 2 是预期研究门槛：查看 quality.json 的 fail / unverified，不能只看进程退出。

以下是已执行的在线采集方式，保留供审阅；当前来源受限，不再执行：

```powershell
& $phase1Python -m ashare_lab.data.cli collect --plan configs/phase1-membership-2023.json --output datasets/raw/phase1-membership-2023 --reuse-root $phase1Raw --sdk-wheel $phase1Wheel
& $phase1Python -m ashare_lab.data.cli plan-2023 securities --run datasets/raw/phase1-membership-2023/run.json --original-plan configs/phase1-membership-2023.json --events configs/csi-events-2023.json --output .cache/phase1/plans/securities-2023.json
& $phase1Python -m ashare_lab.data.cli collect --plan .cache/phase1/plans/securities-2023.json --output datasets/raw/phase1-securities-2023 --reuse-root $phase1Raw --sdk-wheel $phase1Wheel --batch-size 20
```

最新 2023 离线候选构建如下。此输出目录已存在，重放时改用新的目录；不混入代表集内的早年样本。

```powershell
& $phase1Python -m ashare_lab.data.cli build --run datasets/raw/phase1-membership-2023/run.json --run datasets/raw/phase1-securities-2023/run.json --original-plan configs/phase1-membership-2023.json --original-plan .cache/phase1/plans/securities-2023.json --events configs/csi-events-2023.json --output datasets/candidates/phase1-2023-provenance-verified
```

实际输出 75,271 行，返回 2；质量报告明确 1,403 个计划请求中 1,002 个成功、401 个未完成。
证券部分为 943/1,344；缺失请求 ID 在 `quality.json` 的 `acquisition_coverage` 中，完整参数在 raw run
的 `plan.queries` 中。56 个合法零行响应与 400 个未执行请求、1 个未成功请求分开计数。

旧 v1 run 必须显式提供原计划，其文件摘要和嵌入内容均验证后才读取；上面命令保留了本轮原件路径。
新采集使用 v2 run 和同目录原计划副本，无需额外参数。复制 / 归档新 run 时须一起保留
`plan.original.json`。`verify_csi_baseline.py` 也支持 `--original-plan configs/phase1-representative.json`。

年覆盖是已观察 / 已公告代码的并集，不是已认证历史股票池。周度查询发现变化候选，还需公告。
生成并集时包含已核对公告中的股票，避免供应商陈旧快照遗漏年初应有股票。
采集恢复复用同一个 output 和完全相同计划；计划内容变化需新目录。供应商错误停止后先阅读
run.json、events.jsonl 和 work/<attempt>/worker.log；不要自动反复恢复访问限制错误。
网络运行不能同时再开另一套 BaoStock 脚本。全部 worktree 默认共用主仓库
`.cache/phase1/source-access/` 的锁与按中国自然日计数账本，保留它，不通过删除账本重置限制。

数据 / 缓存 / 完整产物位于被忽略的 datasets、artifacts、.cache；版本中只保留小型计划、代码、
测试、文档和紧凑证据。没有模型训练、回测、交易或风控入口。

## 独立补充来源

东方财富补充日线使用独立的 `ashare_lab.fallback.cli`、来源锁 / 账本及候选 schema，
不使用上述 BaoStock 在线命令。首批十只已完成；实际输出位于
`.cache/worktrees/phase1-source-fallback/datasets/`，原件和候选均保留。
入口与错误语义见[补充行情契约](PHASE1_FALLBACK_CONTRACT.md)，
运行记录见[执行报告](PHASE1_FALLBACK_REPORT.md)。

`probe` / `build` 退出 2 仍表示研究资格未满足，不是存储损坏。
已成功首十只无需重新联网；后续只按[固定 90 只续采任务](PHASE1_FALLBACK_CONTINUATION_PLAN.md)
生成新的有身份验证的计划与独立目录。腾讯小样本和深交所上市日期由主代理单独做来源审计，
原件与边界见[核对记录](PHASE1_METADATA_AND_CROSSCHECK.md)。

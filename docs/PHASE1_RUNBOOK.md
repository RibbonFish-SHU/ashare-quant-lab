# Phase 1 本地采集与候选质量检查

使用已验收的 Python 3.11.16 研究环境，工作目录为独立 Phase 1 worktree。
主仓库的 raw 捕获可只读复用。以下路径是本轮实际布局；新输出目录必须不存在。

```powershell
$phase1Python = 'E:\量化\ashare-quant-lab\.venv\Scripts\python.exe'
$phase1Raw = 'E:\量化\ashare-quant-lab\datasets\raw\baostock'
$phase1Wheel = 'E:\量化\ashare-quant-lab\.cache\phase0\data-probe\baostock-0.9.4-py3-none-any.whl'
$env:PYTHONPATH = 'src'
$env:PYTHONIOENCODING = 'utf-8'
& $phase1Python -m ashare_lab.data.cli collect --plan configs/phase1-representative.json --output datasets/raw/phase1-representative --reuse-root $phase1Raw --offline
& $phase1Python -m ashare_lab.data.cli build --run datasets/raw/phase1-representative/run.json --events configs/csi-events-2023.json --output datasets/candidates/phase1-representative
```

代表计划由 `plan-reuse --raw-root <root> --output <new.json>` 生成，精确复用 56 个原始成功请求。
`build` 返回 2 是预期研究门槛：查看 quality.json 的 fail / unverified，不能只看进程退出。

```powershell
& $phase1Python -m ashare_lab.data.cli collect --plan configs/phase1-membership-2023.json --output datasets/raw/phase1-membership-2023 --reuse-root $phase1Raw --sdk-wheel $phase1Wheel
& $phase1Python -m ashare_lab.data.cli plan-2023 securities --run datasets/raw/phase1-membership-2023/run.json --events configs/csi-events-2023.json --output .cache/phase1/plans/securities-2023.json
& $phase1Python -m ashare_lab.data.cli collect --plan .cache/phase1/plans/securities-2023.json --output datasets/raw/phase1-securities-2023 --reuse-root $phase1Raw --sdk-wheel $phase1Wheel --batch-size 20
& $phase1Python -m ashare_lab.data.cli build --run datasets/raw/phase1-representative/run.json --run datasets/raw/phase1-membership-2023/run.json --run datasets/raw/phase1-securities-2023/run.json --events configs/csi-events-2023.json --output datasets/candidates/phase1-2023
```

年覆盖是已观察 / 已公告代码的并集，不是已认证历史股票池。周度查询发现变化候选，还需公告。
生成并集时包含已核对公告中的股票，避免供应商陈旧快照遗漏年初应有股票。
采集恢复复用同一个 output 和完全相同计划；计划内容变化需新目录。供应商错误停止后先阅读
run.json、events.jsonl 和 work/<attempt>/worker.log；不要自动反复恢复访问限制错误。
网络运行不能同时再开另一套 BaoStock 脚本。全部 worktree 默认共用主仓库
`.cache/phase1/source-access/` 的锁与按中国自然日计数账本，保留它，不通过删除账本重置限制。

数据 / 缓存 / 完整产物位于被忽略的 datasets、artifacts、.cache；版本中只保留小型计划、代码、
测试、文档和紧凑证据。没有模型训练、回测、交易或风控入口。

# 历史状态证据离线接入：源码交付

执行代理，2026-10-04（北京时间）。依据 `docs/PHASE1_STATE_EVIDENCE_PLAN.md` 及最新正式交接，
在基线 `007216cfc8f30270210732b86ba73ea9a4e42f66` 创建分支 `phase1/state-evidence`，
工作目录为 `E:\量化\ashare-quant-lab\.cache\worktrees\phase1-state-evidence`。
本报告所在源码提交交主审；主审在独立 review worktree 上完成了 **1 次真实状态候选构建**，
产物与验收记录见 [主审报告](PHASE1_STATE_EVIDENCE_REVIEW.md) 和
`docs/evidence/phase1_state_evidence_main_acceptance.json`。

本批源码、配置、测试、预检进程与报告归执行代理；主代理维护任务书、来源决定、主审和集成。
旧 worktree、原件、BaoStock 限制/账本、失败产物和所有旧候选均保留。没有操作 Herdr、其他项目、网络、服务器或 GPU。
主代理已完成的社区包导入仍是原两次尝试（一次失败、一次成功），本批没有重做社区构建或模型兼容性实验。

## 已实现

新增 `ashare_lab.state_evidence`，提供离线 `validate` / `build` CLI、原件校验、事件/知识时间纯查询、
新命名空间的事件表与日级参考网格、覆盖/冲突/失效来源质量报告。详细字段与限制见
[状态证据契约](STATE_EVIDENCE_CONTRACT.md)，输入及人工选择见
[版本化选择配置](../configs/state-evidence-2023.json)。既有适配器和正式交易/风控逻辑未变。

- 原计划重算证券集合；输入验证实际核对 **71 份文件**（执行代理早期文字曾记为 70，
  以版本化输入验证和真实构建 manifest 为准），合计 47,285,116 字节逐一核对摘要，另记录选择配置自身摘要。
  包含主代理预检的全部 40 份原件/锚点、原计划、原日历 raw、旧候选和原社区失败 manifest。
- XLSX 重解析 7,489 行，与单元格提取及历史事件逐项一致；A/B/D/E 生成历史事件，C 当前简称不输出为历史。
  原 134 个深市代码中 129 个有 270 条截至 2023 年的名称事件；5 个无事件保持未知。
  5 条范围内证券的较晚名称事件排除，未读取这些年份的行情。
- 两份公告、三页正文使用本地 Poppler 重提，与保存提取仅去除空白后全文相同；同时目视核对保留页图。
  标题、代码、ID、附件、事件原句、页码、时段、落款与搜索结果互相绑定。
  688065 的 06-15 下午停牌和 06-26 上午复牌分别建事件；2023-019 的 2022 年落款冲突单独保存。
- 原日历重算 365 个自然日 / 242 个开市日，保留供应商联合日历及未知历史版本限制。
  针对 688065 的原日线 242 行重新核对原始日期、证券、tradestatus、isST、股数及行定位。
  日级标记不被改成全天停牌；353,426 股与下午停牌可并存。
- 共 272 条事件的 `available_time` 全部仍为 null。严格查询默认排除，显式观察时刻政策才允许在本次观察之后使用。
  事后参考、名称连续性、ST 字符串诊断、供应商状态和认证状态分开。`can_trade` / `certified_st` 保持 null。
- 东方财富两个 500 行越期返回及三个 9501 参数错误作为失败覆盖证据保留，采纳事件数为 0。
- 实际执行模块所在 Git 根必须与 `--project-root` 相同；包括状态模块在内的全部项目 Python 源码及状态选择配置
  均核对提交 blob 和当前摘要。主 cwd 加载 worktree 模块时不能把 main 记作执行版本。

本次预检没有发现事件重复、同点冲突或名称链断裂；这不证明历史状态全覆盖或 PIT。
5 个没有名称事件的深市证券为 `000166.SZ`、`000333.SZ`、`001289.SZ`、`001979.SZ`、`300498.SZ`。
本批原公告只覆盖 688065 的一组停复牌；其他证券及大量日期仍没有独立事件证据，正式状态认证覆盖为零。

## 验证与产物

复用主环境：Python **3.11.16**、PyArrow **19.0.1**、DuckDB **1.3.2**、pytest **8.4.1**、Ruff **0.12.2**。
PDF 工具为已有 `D:\texlive\2025\bin\windows\pdftotext.exe`，版本 **25.02.0**，摘要记入预检。
未安装依赖或新建环境。

| 检查 | 结果 |
| --- | --- |
| 新状态测试 | 67 passed，6.54 秒 |
| 短完整测试 | 401 passed，29.52 秒 |
| 主仓库 cwd / worktree PYTHONPATH 下新测试 | 67 passed，6.81 秒 |
| `ruff check src tests scripts` | 通过 |
| 11 个新 Python 文件 `ruff format --check` | 通过 |
| 最终 `validate` 原件与旧候选只读预检 | 退出 0，外部耗时 3.46 秒；无候选构建 |

新测试均自包含，不依赖忽略目录中的真实原件。原件级测试现场生成微型 XLSX、带 Unicode 映射的合成 PDF 和 metadata；
本次没有跳过。纯函数覆盖全天/半日、非零成交、闭市日、未知 availability、晚到复牌、未来生效、显式观察政策、
无事件未知、名称链断裂与未知冲突不可提前泄漏。另覆盖摘要/原句/来源/时段篡改、重复文件/事件、公式、空查询、
错误 root、Git 隐藏未提交源码变更、原候选不变、失败产物保留、Parquet/DuckDB 往返与正式读取器拒绝。
合成构建只验证工程行为，不是实际股票候选结果。

准确 argv、cwd、PID、起止/耗时和退出码在本 worktree：
`artifacts/state-evidence-source-checks/20261003T182654Z/execution.json`。
同目录保留 `new-tests.log`、`full-tests.log`、`main-cwd.log`、`ruff.log`、`format.log`、
`input-validation.log`、`input-validation.json` 与 `source-files.json`。
紧凑索引见 [源码检查证据](evidence/phase1_state_evidence_source_checks.json)。

开发期第一次只读预检因代码漏处理原提取中的 5 个 null 开始参考而失败；现已修复并由正例/反例覆盖。
该本地实现错误记录保留在 `.cache/state-evidence/preflight-01.log`，不算供应商响应或候选构建失败。
`.cache/state-evidence/preflight-02.json` 保留修复后预检；最终结果以版本化检查索引所绑定的日志为准。

## 主审实际构建与验收

```powershell
$stateRoot = 'E:\量化\ashare-quant-lab\.cache\worktrees\phase1-state-review'
$inputRoot = 'E:\量化\ashare-quant-lab'
$env:PYTHONPATH = Join-Path $stateRoot 'src'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location $stateRoot
& 'E:\量化\ashare-quant-lab\.venv\Scripts\python.exe' -m ashare_lab.state_evidence.cli build `
  --source "$stateRoot\configs\state-evidence-2023.json" `
  --input-root $inputRoot `
  --pdftotext 'D:\texlive\2025\bin\windows\pdftotext.exe' `
  --project-root $stateRoot `
  --output "$stateRoot\datasets\candidates\state-evidence-2023-main-review-original336-v1"
```

主审在干净提交 `2be10c58983a172ea99e3e5f5f3eb8cdd87cc937` 的独立 worktree 上执行上述命令一次。
CLI 返回 **2**，耗时 **11.600687999976799 秒**；manifest 为 `built_candidate`，质量为 `unverified`，
正式读取器拒绝，且 `research_eligible=false`。候选路径为
`datasets/candidates/state-evidence-2023-main-review-original336-v1`（位于主审 review worktree）。
输出含 272 条事件和 81,312 条日级参考（336 个证券 × 242 个开市日），events/daily_references
均通过 Parquet 与 DuckDB 往返。688065 的 2023-06-15 被标为半日停牌（当日成交 353,426 股），
2023-06-16、06-19、06-20、06-21 为完整停牌日，2023-06-26 复牌参考不会改写严格 as-of 视图。
所有事件的 `available_time` 仍为 null，`can_trade` 仍为 null，认证状态覆盖为零。

构建子进程已正常退出，保护输入前后摘要一致，未联网、未使用 GPU、未执行服务器、模型或回测任务。
审阅封装脚本在构建完成后首次写入 `protected-after.json` 时把 Windows `Path` 当作 JSON 键，
因此自身先以 `TypeError` 退出；本次没有重跑构建，主审通过只读后验补齐执行记录并保留该问题说明。
完整 argv、进程、输入保护摘要、manifest/表摘要和限制见主审报告及版本化验收证据。

本轮检查、构建与后验子进程均已退出，无常驻采集/实验进程。候选仍为不可研究的历史参考，
不代表 PIT、停牌/风险警示全覆盖或正式研究数据验收；2024–2025 行情、模型、回测、GPU 和服务器任务均未执行。

# Phase 1 社区历史包离线导入执行报告

执行代理；基线 `48cc074386fc87c75b293a6982121a51d21b58a0`，分支 `phase1/community-import`。
本轮实现、测试、离线构建及产物由执行代理负责；主代理负责任务书、外部来源、审阅与集成。
所有旧 worktree、原件、候选、失败和来源限制只读保留。

## 源码阶段

新增 `ashare_lab.community` 离线入口、版本化来源配置、候选契约及自包含负例测试。
详见 [候选契约](PHASE1_COMMUNITY_IMPORT_CONTRACT.md)。复用主仓库 Python 3.11.16 环境，
未安装或修改依赖。归档只做流式检查与指定字段读取，不执行上游脚本。

输入归档为主仓库 `datasets/raw/investment_data/2023-12-31/qlib_bin.tar.gz`，
407,271,371 字节，SHA256 `5651eb91463e1cee56ad23a8fe41bc2036fcae54dd5720a6c6ddc238e2b3a4b3`。
来源配置绑定 release 135431362 / asset 143134949、tag commit、实际下载 metadata、原 336
请求计划及三个已审行情候选。实现重算覆盖和差异，不将既有核验数字写成成功常量。

源码阶段 63 项新增测试、317 项完整离线测试、Ruff 及格式检查已通过；从主仓库 cwd 执行
新增测试同样为 63 passed，消除了 worktree 路径层级依赖。完整测试 16.01 秒，主 cwd 1.94 秒；
日志与准确命令/PID/耗时在 `artifacts/community-checks/`。未运行 Qlib 模型或兼容性实验。
负例包含来源状态与身份矛盾、摘要变动、重复/恶意
归档成员、gzip 损坏/截断、资源超限、float32 偏移及日历边界、零因子/无限值、修改当前报价、
上市前和停牌参考区别、未知单位、同源/重复参考、原件不变及旧输出不可覆盖。
输入预检已验证 54 份绑定文件，包括整个归档摘要；从原计划重算为 336 代码，加载到 81,296
个旧行情位置、336 个 IPO 参考及 29 行请求区间内的腾讯补充参考。尚未生成真实候选。
正式运行原定在稳定源码提交后执行一次；2026-10-04 主审更新为**先交稳定提交和启动命令，
由主审检查后再启动**。目前真实候选构建次数为 0；没有真实运行结果或完成验收声明。

## 构建前主审修复

1. 原实现只对传入的 `project_root` 做 `code_identity`，没有绑定 PYTHONPATH 实际模块。
   现在先解析 `pipeline.__file__`，确认其所属 Git worktree 根等于传入根；在错误根上不调用
   `code_identity`。全部 community `.py` 必须在源码摘要中存在且一致，再从该 worktree 的
   精确 commit 读取文件原文核对，仅允许 CRLF/LF 等价；工作文件与 commit blob SHA256 都记录。
   新测试建两个真实临时 Git worktree：从 main cwd 加载执行分支模块时，默认/显式错误根均
   拒绝，显式执行根通过；`assume-unchanged` 隐藏改动即使状态干净也被提交原文检查拒绝。
2. 入口明确只支持锁定的 2023-12-31 tag，且将校验后的 `release_latest` 传入归档读取和
   成员诊断。伪造相互一致的 2023-01-04 release metadata 却保留 1 月 5 日归档的 fixture
   被拒绝，另测试归档日历和成员区间确实受传入版本日期约束。

修复后检查：完整测试 **325 passed / 18.02 秒**；从主仓库 cwd 执行新增测试 **71 passed /
7.64 秒**；全仓库 Ruff 和八个 Python 文件格式检查通过。新测试均自包含，无真实数据依赖
或 skip。准确 argv / PID / 退出码 / 耗时及日志在 `artifacts/community-review-checks/`；
原先 317 项日志原样保留。测试子进程均已等待正常退出。真实候选构建次数仍为 **0**。
版本化源码检查索引见 `docs/evidence/phase1_community_import_source_checks.json`。

本轮曾有配置生成命令的 PowerShell stdin 编码将中文路径替换为 `??`，在读取前失败；
改为从 Git common-dir 显式 UTF-8 发现路径后成功。一次 `rg tests/test*.py` 的 Windows 通配
路径错误已改用 `rg ... tests -g '*.py'`。二者均为本地命令错误，没有供应商响应或联网行为。
检查源码绑定时，另有 `git -C` 错传 Python 文件路径，以及对尚未提交的新文件执行
`git show HEAD:path` 的本地诊断失败；二者说明命令目标需使用实际模块目录/提交，没有读取
供应商或影响原始数据。新实现与真实临时 worktree 回归已覆盖正确目标。

## 拟执行命令（主审检查后单次启动）

cwd 为本 worktree；复用主仓库解释器，显式指定 PYTHONPATH 与执行根。

```powershell
$communityMain = 'E:\量化\ashare-quant-lab'
$communityWork = 'E:\量化\ashare-quant-lab\.cache\worktrees\phase1-community-import'
$env:PYTHONPATH = "$communityWork\src"
$env:PYTHONIOENCODING = 'utf-8'
& "$communityMain\.venv\Scripts\python.exe" -m ashare_lab.community.cli `
  --archive "$communityMain\datasets\raw\investment_data\2023-12-31\qlib_bin.tar.gz" `
  --source "$communityWork\configs\community-2023.json" `
  --input-root $communityMain `
  --plan "$communityMain\.cache\worktrees\phase1-data\.cache\phase1\plans\securities-2023.json" `
  --start 2023-01-01 --end 2023-12-31 `
  --project-root $communityWork `
  --output "$communityWork\datasets\candidates\community-2023-original336"
```

运行前后的旧输入、来源账本与限制记录会另做摘要快照；源 SHA、准确 argv、PID、耗时、输出
摘要与未知事项写入最终证据。当前源码/测试/计划启动过程由执行代理保有；未建立本轮后台
任务。main 475d200 的交易状态来源证据按主审指示留待后续批次，本批不接入。

## 当前边界

候选、质量、成员参考均不晋升研究资格；不将下载或 release 时点当作历史可用时间。
没有联网、BaoStock 重试、最新包、2024–2025 行情、模型、回测、服务器、GPU 或公司行为扩采。
保留社区原 csi300 区间，条件公告对照明示假设，不输出认证股票池。

# Phase 0 工程批次执行交接

执行者：`ashare-experiment`（已核实 `w8:p2`）；日期：2026-10-03。
结论：**本地供应商无关工程批次通过；Phase 0 整体验收尚未完成。**

## 交付与版本

首次实现提交：`b6cfec917cc2322c7ed1a77a66286611c1e4fce6`。
首次报告与 evidence 提交 `e668294` 仅记录该版本的验证；后续主审修复及新增测试见本文末节。
主代理原有 AGENTS、PROJECT_PLAN、Phase 0 / 交接 / 数据源文档已随稳定批次保留并纳入版本。
原始方案工作区文件的 SHA-256 仍为
`3b2ae7232edd5c4e4156cdf4c7440b1d36c3814d257f40ec182128315777c147`。

- 独立 `.venv` 与项目内 CPython 3.11.16；完整 uv.lock，Windows CPU 已安装并实际调用。
- TOML 配置、固定 seed、结构化日志、独立运行目录、源码 / 数据 / 特征 / 模型版本与失败证据。
- 行情、交易日历、历史成员、公司行为、状态五表契约，UTC / 上海日期、修订键与可用时间检查。
- 不可覆盖的 Parquet 数据集、摘要核对和 DuckDB 可用时点查询；synthetic / real 命名空间隔离。
- 下一交易日开盘、五个交易间隔、标签成熟时间、实际跨界区间和预处理拟合日期检查。
- CPU / GPU UUID 选择入口，预约确认前拒绝 GPU 运行；即时忙碌检查与只读设备枚举接口。
- [运行说明](RUNBOOK.md)、[数据契约](DATA_CONTRACT.md)、有针对性的测试。

## 首次批次实际验证

以下检查均在本项目 `.venv` 执行，退出码为 0。测试对应实现提交中的最终源码，
没有因生成本报告而重复运行测试。

| 命令 | 结果 |
| --- | --- |
| `.venv/Scripts/python.exe -m pytest -q` | **42 passed in 1.56s** |
| `.venv/Scripts/ruff.exe check src tests` | All checks passed |
| `.venv/Scripts/ruff.exe format --check src tests` | 15 files already formatted |
| `.cache/phase0/bootstrap/Scripts/uv.exe pip check --python .venv/Scripts/python.exe` | 189 个已安装包依赖一致 |
| `uv lock --check --offline --python .venv/Scripts/python.exe --cache-dir .cache/phase0/uv-cache` | 锁文件与项目一致，206 个跨平台解析包 |
| `uv build --wheel --python .venv/Scripts/python.exe --cache-dir .cache/phase0/uv-cache` | wheel 构建成功；17,702 字节、15 个条目，仅代码和包元数据 |
| `.venv/Scripts/python.exe -m ashare_lab.cli smoke --config configs/smoke.toml` | 已提交且 clean 的源码运行通过，完整子进程耗时 **5.813 秒** |

后两条 `uv` 实际使用 `.cache/phase0/bootstrap/Scripts/uv.exe`。
wheel 位于被忽略的 `dist/ashare_quant_lab-0.1.0-py3-none-any.whl`。
构建工具提示缓存位于源码树内；已打开 wheel 核对，缓存、环境、数据和产物均未打包。

最终 smoke 目录：
`artifacts/experiments/synthetic/20261003T062541.770495Z-84e985e0/`。

运行时 manifest 确认提交为上述 SHA、`dirty=false`、`status=passed`；紧凑证据已归档：

- [版本与源码清单](evidence/phase0_local_smoke_manifest.json)
- [实际结果](evidence/phase0_local_smoke_result.json)
- [命令、退出码、耗时及控制台日志](evidence/phase0_local_smoke_execution.json)

smoke 验证了 73 条行情（含一次晚到修订）、48 条日历、4 条成员区间修订、1 条公司行为、72 条状态。
五表 Parquet 精确往返；DuckDB 与参考时点选择一致；未来数据拒绝。
Qlib 从本次合成原生 provider 读取 24 日 close 并核对 Ref 滞后一日表达式，
Alpha158 配置产生 158 个表达式（**没有运行正式 Alpha158 股票基线**）。
LightGBM 在 64 行合成数值上拟合 / 推理，PyTorch 在 CPU 上完成 3 步小张量训练。
这些数值仅是库兼容性证据。

最终 smoke 的五表文件摘要及库调用结果，与修复中文路径后前一次运行完全一致。
两次运行之间增加的是时间边界防护 / 测试和文档；此核对不构成跨平台数值一致性承诺。
另有独立测试比较相同 seed 的五表 Parquet 字节，且确认不同 seed 会改变样例。

## 失败与恢复

1. 首次 smoke 的 LightGBM 原生 `save_model` 无法写入 Windows 中文路径。
   失败保留在 `artifacts/experiments/synthetic/20261003T061631.762503Z-25d10568/`
   的 manifest、events 和 traceback。改为 `model_to_string()` + Python UTF-8 写入后通过；
   PyTorch 同样经 Python 二进制句柄保存，避免路径兼容问题。
2. 静态检查最初发现测试中的 lambda 赋值风格问题，已修复，最终检查通过。
3. 初次 Git commit 因当前仓库没有作者配置失败；核对初始化提交作者与 `gh api user` 的
   当前 RibbonFish-SHU 身份一致后，仅在本仓库设置已有提交身份，再完成提交。没有改全局配置。
4. 工具子进程没有继承 Herdr 环境变量；`--current` 实际返回主代理焦点 pane，不能据此认定自身。
   后续只对 `ashare-main` / `ashare-experiment` 作显式 get，结合已交接 pane / terminal 信息定位，
   没有伪造变量，没有操作其他项目或自行创建代理。

## 服务器与资源证据

本轮仅作只读检查。服务器项目仍为初始化提交，未同步或执行本批次源码，也没有安装大型依赖。

| 项目 | 实际观察 |
| --- | --- |
| 系统 | Ubuntu 20.04.6 LTS，系统 Python 3.8.10，已有 uv 0.12.17，64 个逻辑 CPU |
| GPU | 8 张 NVIDIA RTX A5000，驱动 535.261.03；查询时各卡 0% 利用率、9 MiB 已用，无计算进程 |
| 显存报告 | 索引 0/1/4/6 为 23028 MiB；2/3/5/7 为 24564 MiB，按驱动输出记录，不把全部写成统一实测容量 |
| 内存 | 当时约 181 GiB available；swap 2 GiB 已满，仅作现场记录 |
| 项目挂载 | `/home/user`，`/dev/sdb`，ext4，`rw,relatime,stripe=64` |
| 可用空间 | **20,826,722,304 字节**；df 使用率显示 100% |
| inode | 总 219,721,728，已用 17,569,981，可用 202,151,747（8%） |
| 原项目目录体积 | 检查时 96,386 字节 |
| 配额 / 预约 | 未找到 quota、Slurm / PBS 命令；未核实外部配额或预约规则，不能推定无配额 / 无预约 |
| 网络 | PyPI、Tsinghua 镜像、GitHub、PyTorch 官方源的 HTTPS 连接被重置（curl 35） |

检查命令包括 `findmnt -T`、`df -B1`、`df -i`、`du -sb`、`free -h`、
`ps -eo user,pid,pcpu,pmem,comm --sort=-pcpu`、`ss -ltn`、
`nvidia-smi --query-gpu=... --format=csv` 和 `--query-compute-apps=...`；
没有结束、抢占或复用他人进程，没有开放服务端口。

依主代理资源决定，**GPU smoke 暂缓**。没有建立新的 CUDA 环境、传输大型包或清理共享盘。
Linux/CUDA 依赖锁已解析，但安装峰值 / 配额、Linux 运行兼容性及单卡训练未验收。
若后续恢复，先明确预约归属，再估算 wheel、解压环境、缓存峰值并重新检查容量；
需要离线传输时仅传输项目自己的校验过依赖与已知 Git 提交。

## 交接边界

源码和本轮本地产物由执行代理完成；主代理负责审阅、集成及对外进度。
本轮所有命令和测试已退出，**没有活动的实验 / 下载进程，没有 GPU 分配或后台服务**。
本地环境和 `.cache/phase0/` 保留便于复现；原始失败证据保留。
本批次尚未推送 GitHub 或同步服务器，主代理可审阅本地稳定提交后决定必要同步。

仍需真实数据来源 / 权限、历史起止、状态 / 成员覆盖验收、正式标签 / 费用口径与冻结划分。
候选 2010–2025 等区间只是主代理草案，本轮未获取任何真实行情或查看最终封存数据。
下一步是主代理审阅工程批次并处理上述真实数据与资源依赖；不能宣布整个 Phase 0 完成。

## 主审一致性修复（2026-10-03）

依据主代理的 [PHASE0_REVIEW.md](PHASE0_REVIEW.md)，在 `e668294` 基础上完成两个输入一致性修复。
主代理准备的审阅文件原样纳入本批次；本节记录执行结果，最终集成仍由主代理审阅决定。

- **退市状态**：`delisted_date` 是首个已退市的上海自然日，包含生效当天。
  当日及之后只能是 `delisted`；该状态必须提供不晚于 event_date 的退市日期。
  已公告但尚未生效的未来退市日期仍可配合 `active` / `suspended`；既有上市日与停牌一致性检查继续适用。
- **日历来源**：按 signal 时点取可见修订并选择 exchange 后，明确要求唯一 source，再过滤开市日。
  两来源交替提供不重叠日期、或第二来源仅有休市日，均被拒绝。
  其他交易所、尚不可见的来源不会干扰当前查询，同源版本修订与休市日仍合法。
- **测试**：新增 18 个参数化 / 边界案例。修复前运行新增案例得到 8 failed、10 passed，
  复现了 6 组矛盾退市状态及 2 组此前漏检的日历来源组合。
  修复后这些案例全部通过，覆盖退市日前 / 当日 / 后、缺日期、未来日期，以及来源筛选的顺序和可用时间等号边界。

| 实际命令 | 结果 |
| --- | --- |
| `.venv/Scripts/python.exe -m pytest -q tests/test_contracts.py tests/test_temporal.py -k 'delisting or calendar_source or visible_sources or single_source_calendar'` | **18 passed, 29 deselected in 0.52s** |
| `.venv/Scripts/python.exe -m pytest -q` | **60 passed in 1.60s**，一次短完整回归 |
| `.venv/Scripts/ruff.exe check src tests` | All checks passed |
| `.venv/Scripts/ruff.exe format --check src tests` | 15 files already formatted |

检查记录见 [本轮命令结果](evidence/phase0_review_fix_checks.json)。
本次未改依赖、库调用、模型训练或打包配置，复用原版本的库兼容性证据，没有再次运行完整模型 smoke。
对实际涉及的新校验路径做了一次只读数据 / 时间验证：用新契约读取原 smoke 的五表，
比较 DuckDB 与参考 as_of、历史成员及 next-open 标签端点，结果与原始 result 一致。
核对原成功运行与归档 evidence 的文件摘要，均未变化；
见 [数据 / 时间检查记录](evidence/phase0_review_fix_data_check.json)。原始成功 / 失败产物继续保留。

本修复没有 GPU、真实数据、网络采集或服务器执行；完整 Phase 0 的数据与资源缺口保持未验收。
所有本轮检查已退出；修复提交仅留在本地供主代理审阅，未推送或同步服务器。

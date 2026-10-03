# Phase 0 Linux 执行报告

Linux 独立环境、CPU smoke 和已授权单卡 CUDA smoke 均通过。本轮使用合成样本验收工程行为；
真实数据读取、历史覆盖及冻结时序仍由主代理推进，不能据此宣称整个 Phase 0 已验收。

## 范围与版本

- 基线：`f029e8e39f608e649bc4d81aad6da439ea9d67d6`。
- 实际安装与运行提交：`277d577aeeb7dd2676dc2f22d9dd82edca596541`，运行时工作区干净。
- 本地分支：`phase0/linux-validation`。
- 本地 worktree：`E:\量化\ashare-quant-lab\.cache\worktrees\phase0-linux-validation`。
- 服务器 worktree：`/home/user/dyy_work/ashare-quant-lab/.cache/worktrees/phase0-linux-validation`。
- 执行身份：`ashare-experiment = w8:p2`。本轮环境、下载、传输、测试和训练均由执行代理管理。
- 原 `uv.lock` 的 Linux CUDA 11.8 profile 保留；没有变更运行依赖版本或依赖架构。
- 解释器：CPython 3.11.16，Astral python-build-standalone 官方精简发行包。
- 核心依赖：numpy 1.26.4、pandas 2.2.3、pyarrow 19.0.1、duckdb 1.3.2、
  pyqlib 0.9.7、LightGBM 4.6.0、torch 2.7.1+cu118、mlflow 2.22.1；
  pytest 8.4.1、Ruff 0.12.2，uv 0.12.17。
- 实际平台：Ubuntu 20.04.6、x86_64、glibc 2.31、Linux 5.15.0-139-generic；NVIDIA 驱动 535.261.03。

完整安装后版本、源码摘要和依赖检查见 [最终环境证据](evidence/phase0_linux_final_environment.json)。
198 个依赖版本与选定 Linux 锁一致，加项目本体共 199 个安装包。

## 离线准备与容量

项目位于 `/dev/sdb` 的 ext4 `/home/user` 挂载。初次检查可用 20,820,316,160 字节；
传输前复查为 19,818,307,584 字节。可用 inode 超过 2 亿；可用内存约 195 GB，
存在其他用户 CPU 任务，未干预其进程。没有启动监听服务。
未发现 quota/repquota/quotaon 工具，挂载选项未显示 quota；用户配额没有被独立核实。
安装采用实际可用字节门槛，本次没有遇到配额或空间错误。

从原锁选择 198 个 Linux 运行/开发依赖：197 个使用官方 wheel；Gym 0.26.2 只有官方 sdist，
使用固定 build-only setuptools 80.9.0 / wheel 0.45.1 构建无原生二进制的通用 wheel。
源包和派生 wheel 摘要均保留；运行时 setuptools 仍为原锁的 84.0.0。
解释器 SHA-256 取自 uv 0.12.17 官方下载元数据。
完整 URL、摘要、压缩/解压大小见 [发行包证据](evidence/phase0_linux_artifacts.json)。

| 容量项目 | 字节 |
| --- | ---: |
| 198 个原始依赖发行包 | 3,237,157,111 |
| 解释器压缩包 / 解压内容 | 30,778,779 / 82,119,687 |
| 198 个安装 wheel 解压内容 | 6,622,157,316 |
| 最大 wheel 解压内容（torch） | 1,750,594,784 |
| 传输发行包（含 Gym 源/轮、解释器、项目 wheel） | 3,268,781,489 |
| 安装前实测剩余空间（包已传完） | 16,549,441,536 |
| 安装增量保守预算 | 9,908,464,506 |
| 按预算预计仍可保留 | 6,640,977,030 |
| 预留容量下限 | 5,368,709,120（5 GiB） |
| 安装期间最小采样剩余空间 | 9,570,697,216 |
| GPU 验收后、清理前项目占用 | 10,249,449,472 |
| 清理后项目占用（16:27:43 +08:00 快照） | 6,936,887,296 |
| 同一快照的挂载剩余空间 | 12,882,972,672 |

安装按 wheel 串行解压，uv 缓存与 `.venv` 同挂载，以 hardlink 共享文件。
预算为解释器 + 全部 wheel 解压 + 再多一份最大 wheel + 每归档成员 8192 字节余量 + 1 GiB。
直接传目录，没有在服务器保留第二份大型传输归档。
安装期间按 0.25 秒采样可用空间，共 184 次；每步骤结束记录实际 `du`。
这些值是采样及阶段末观测，不能证明瞬时绝对峰值；共享挂载的空间变化也可能来自其他任务。
详见 [安装记录](evidence/phase0_linux_installation.json) 和 [清理前快照](evidence/phase0_linux_pre_cleanup.json)。

服务器直连 PyPI/GitHub/PyTorch 遇到 TLS reset，改为本地下载、逐包校验后 SSH 传输。
下载期间截断响应被大小/摘要检查拒绝；PyTorch 官方 R2 URL 改用官方 `download.pytorch.org` 同路径，
得到与原锁完全相同的 SHA-256。未使用第三方替代包或放宽摘要检查。
Windows CRLF 与 Git LF 的差异在传输前处理：锁身份以 Git blob / LF 为准，下载主机摘要另存。
项目 wheel 从禁用 autocrlf 的 Git archive 构建，10 个 Python 源文件逐字节匹配运行提交；
安装后再次核对相同的 10 个文件。[传输与构建证据](evidence/phase0_linux_transfer.json) 记录了准确摘要。

## 命令与实际检查

本地 uv 为 `E:\量化\ashare-quant-lab\.cache\phase0\bootstrap\Scripts\uv.exe`，
构建 Python 为同项目 `.venv\Scripts\python.exe`（3.11.16），没有采用系统默认 Python 3.14。
以下命令中的 `uv` / `python` 均指这两个明确路径：

```powershell
uv export --frozen --extra dev --no-emit-project --format requirements-txt --output-file .cache/phase0-linux/requirements-export.txt
python scripts/prepare_linux_offline.py --export .cache/phase0-linux/requirements-export.txt --output .cache/phase0-linux/offline --download
uv build .cache/phase0-linux/offline/distributions/gym-0.26.2.tar.gz --wheel --out-dir .cache/phase0-linux/built --build-constraints configs/linux-build-constraints.txt --python <project-python-3.11.16> --cache-dir .cache/phase0-linux/uv-cache
git -c core.autocrlf=false archive --format=zip --output=.cache/phase0-linux/source-lf-277d577.zip 277d577
# 将归档解压到独立 build-source-lf-277d577 后构建项目 wheel：
uv build .cache/phase0-linux/build-source-lf-277d577 --wheel --out-dir .cache/phase0-linux/built --build-constraints configs/linux-build-constraints.txt --python <project-python-3.11.16> --cache-dir .cache/phase0-linux/uv-cache --offline
```

`offline/bundle.json` 由已核验的发行包元数据和项目 wheel 度量组成，包含运行提交、锁摘要、
201 个传输文件的摘要，以及解释器/运行依赖/项目 wheel 的解压度量。
该清单及原始官方包保存在本地任务缓存中，服务器安装器先校验清单身份和全部发行包再安装。
Git bundle 只同步本任务分支到服务器 worktree，未推送 GitHub，未修改 main 或 origin/main。

```bash
cd /home/user/dyy_work/ashare-quant-lab/.cache/worktrees/phase0-linux-validation
python3 scripts/install_linux_offline.py --manifest .cache/phase0-linux/offline/bundle.json --commit 277d577aeeb7dd2676dc2f22d9dd82edca596541 --reserve-gib 5
```

| 检查 | 结果 |
| --- | --- |
| 本地完整 pytest / Ruff | 65 passed in 1.88s / 通过 |
| Linux 解释器、独立 venv、198 包 hash lock 安装、项目 wheel | 全部退出 0；依赖安装 20.536 秒 |
| Linux `uv pip check` | 通过 |
| Linux 完整 pytest / Ruff | 65 passed in 3.64s / 通过 |
| Linux CPU 完整 smoke | 退出 0；进程耗时 16.479 秒 |
| 最小单 GPU 完整 smoke | 退出 0；含采样包装器耗时 8.268 秒 |
| 清理后依赖、核心库导入、版本与安装源码核对 | 全部通过 |

新增 5 项测试覆盖解释器正常解压、拒绝覆盖已有目录、路径/链接越界和特殊归档条目。
安装和 CPU 检查设置 `CUDA_VISIBLE_DEVICES=''`，OMP/MKL 限制为 1 线程。
[逐项原始日志](evidence/phase0_linux_logs/) 保留命令结果；服务器运行源码与此前核心实现一致。
两次 smoke 均验证了五表 Parquet 往返、DuckDB 时点查询、未来特征拒绝、次开盘标签边界、
64 行 LightGBM 拟合/预测、3 步 PyTorch MLP，以及 Qlib 原生合成 provider 的 24 行读取和滞后表达式。
Alpha158 检查只验证 158 个表达式配置，不是完整 Alpha158 训练或真实数据回测。

## GPU 授权与结果

用户本轮明确授权“本项目可以使用当前空闲卡”，主代理转达并撤销此前 GPU 暂停决定。
16:21:55 +08:00 重新枚举 8 张 NVIDIA RTX A5000，均为 0% 利用率、9 MiB 占用、无计算进程。
选择 GPU 0：`GPU-c59b5457-918c-909d-424d-afb0736b687a`，可报告显存 23,028 MiB。
8 卡完整 UUID、不同可报告显存及状态见 [GPU 运行观察](evidence/phase0_linux_gpu_observation.json)。

```bash
.venv/bin/python scripts/validate_linux_gpu.py --commit 277d577aeeb7dd2676dc2f22d9dd82edca596541 --gpu GPU-c59b5457-918c-909d-424d-afb0736b687a --allocation-confirmed --authorization 'User 2026-10-03 explicitly authorized this project to use currently idle GPUs; ashare-main relayed authorization and revoked the prior GPU pause.'
```

包装器与实际 CLI 都在启动前即时检查选卡空闲状态。训练子进程 PID 为 `3627512`，
可见设备限定为该 UUID，PyTorch 确认只有 1 张可用设备，以 `cuda:0` 执行 3 步训练。
采样观测到整卡最高 382 MiB、本进程 368 MiB；这不是 CUDA 分配器精确峰值。
退出后立即检查：无计算进程、显存恢复 9 MiB，利用率窗口尚显示 5%；16:24:32 及 16:27:43
复查时全部 8 卡均为 9 MiB / 0%。其他卡没有观察到本任务计算进程。
该结果覆盖 8 卡枚举、明确 UUID 选择和单卡训练；多卡训练不在本轮验收范围。

## 产物、清理与交接

服务器原始产物均保留在上述 worktree：

- CPU：`artifacts/experiments/synthetic/20261003T082111.238796Z-aaf5bcb1/`。
- GPU：`artifacts/experiments/synthetic/20261003T082227.336319Z-04473326/`。
- 解释器：`.cache/phase0-linux/interpreter/python/bin/python3.11`。
- 环境：`.venv/bin/python`，环境不可脱离当前解释器目录单独搬迁。
- 原始日志：`.cache/phase0-linux/logs/`；版本清单和数据/模型原始文件保留在各 run 目录。
- 本地缓存 `.cache/phase0-linux/` 保存官方包、派生 wheel、下载记录、转储日志和两次运行的完整副本。
- Git 中保存两次运行的原样 manifest/result/events；每份 manifest 的 42 个源码摘要均与 `277d577` 的 Git blob 匹配。

清理前核对了精确路径、归属与硬链接：`libtorch_cuda.so` 的链接数由 2 变为 1，inode 与大小不变。
仅清理本任务 uv 缓存、服务器已验证的 `offline/distributions` 及已验证的小型 Git bundle。
本地官方包、服务器解释器、`.venv`、日志、运行产物和原有项目证据均保留。
清理后再次导入 PyTorch/Qlib/LightGBM/DuckDB/Arrow 并核对安装包，未重复训练。
一次本地日志收集遇到编码错误；只读复查确认清理尚未发生后，以原始字节传输执行并保存
[清理日志](evidence/phase0_linux_logs/cleanup.log)，没有凭失败消息重复删除。

截至最终环境快照，CPU/GPU 运行 PID 均已退出，下载/传输/安装任务均已结束，无遗留训练或常驻汇报进程。
服务器任务 worktree 保留实际运行提交 `277d577`；本地后续报告提交仅增加证据和文档。
主代理可审阅本地任务分支后决定集成。main、origin/main 和服务器主工作区继续由主代理管理。

本轮收尾的命令工具未继承 `HERDR_ENV=1`，因此遵守 Herdr 技能的环境检查要求，
未从该工具控制 pane 或直接发送交接。结果通过本报告、任务分支和最终回复交付；
不将“交接内容已准备”记作“Herdr 消息已提交”。

剩余验收：真实供应商数据完整读取、历史成分/状态覆盖核验、时序与封存规则冻结。
用户已授权主代理核验免费数据；本轮未批量获取真实行情、未运行正式模型评估或触及交易/风控。

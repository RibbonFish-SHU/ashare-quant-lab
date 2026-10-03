# Phase 0 运行说明

所有命令从项目根目录执行。本地 `E:\量化\ashare-quant-lab`；服务器仅使用
`lynsdu2@10.0.33.75:/home/user/dyy_work/ashare-quant-lab`。不复用其他项目环境。

## 兼容环境

本轮选 CPython **3.11.16**。PyPI 的 Qlib **0.9.7** 有 cp311 Windows / manylinux 二进制发行包；
本地默认 3.14 未被选用，服务器系统 3.8.10 未被修改。3.11 同时覆盖 Qlib、LightGBM、
Arrow、DuckDB 和 PyTorch 的已发布二进制轮子，最终以实际调用验证兼容性。

| 直接依赖 | 锁定版本 | 用途 |
| --- | --- | --- |
| numpy / pandas | 1.26.4 / 2.2.3 | 避免 Qlib 旧版扩展切换到 NumPy 2 ABI；数据互操作 |
| pyqlib | 0.9.7 | 本地原生 provider 和 Ref 表达式；Alpha158 的 158 个表达式配置 |
| lightgbm | 4.6.0 | 合成数值的 4 棵树拟合 / 推理及模型持久化 |
| torch | 2.7.1 | 3 步小张量训练；Windows CPU / Linux cu118 发行源 |
| pyarrow / duckdb | 19.0.1 / 1.3.2 | Parquet 和 SQL 时点查询 |
| mlflow | 2.22.1 | 锁定 Qlib 使用的实验依赖，避免自动迁移到新主版本 |
| pytest / ruff | 8.4.1 / 0.12.2 | 有针对性的行为测试及静态检查 |

`uv.lock` 锁定完整传递依赖及下载摘要，`pyproject.toml` 约束 Python 3.11。
uv **0.12.17** 用于本轮环境解析与安装。Linux 的 cu118 源是针对现有驱动 535.261.03 的兼容候选，
不能把锁文件解析成功当作 Linux / CUDA 运行验收；以最终执行报告为准。

本地没有全局 uv 时，本轮实际引导命令如下。3.14 只用于隔离安装包管理器，不承载研究依赖：

```powershell
python -m venv .cache/phase0/bootstrap
.cache/phase0/bootstrap/Scripts/python.exe -m pip install --disable-pip-version-check uv==0.12.17
.cache/phase0/bootstrap/Scripts/uv.exe python install 3.11.16 --install-dir .cache/phase0/python --cache-dir .cache/phase0/uv-cache --no-bin
.cache/phase0/bootstrap/Scripts/uv.exe sync --frozen --extra dev --python .cache/phase0/python/cpython-3.11.16-windows-x86_64-none/python.exe --cache-dir .cache/phase0/uv-cache
```

## 检查与运行

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check src tests
.venv/Scripts/ruff.exe format --check src tests
.venv/Scripts/python.exe -m ashare_lab.cli smoke --config configs/smoke.toml
```

等效的已安装入口为 `.venv/Scripts/ashare-lab.exe smoke`。配置拒绝未知 / 缺失键、非法 seed、
真实数据 smoke 和逃出项目 artifacts 的输出目录，错误返回非零状态并给出字段原因。
运行时异常保留 `manifest.json(status=failed)`、`traceback.txt` 和 `events.jsonl`。
配置无法解析时尚未建立运行目录，错误以 JSON 输出到 stderr。

每次运行在 `artifacts/experiments/synthetic/<UTC时间>-<随机ID>/` 保存：

- `manifest.json`：配置、seed、数据 / 特征 / 模型版本、Python / 全部依赖、源码提交、dirty 状态、
  Git 管理与未忽略文件的 SHA-256 清单及总体摘要；未提交运行不会伪称精确提交可重放。
- `events.jsonl`、`result.json` 或失败栈：存储检查、设备选择、运行状态；无策略收益输出。
- `data/synthetic/<version>/`：五表 Parquet、数据 SHA-256 清单。
- `qlib_synthetic/`：当次 fixture 的 Qlib 原生数据，严格独立。
- `lightgbm-smoke.txt`、`torch-smoke.pt`：纯工程模型产物，无投资或股票预测含义。

CPU 随机数显式固定，PyTorch 启用 deterministic algorithms，计算线程为 1。
相同环境 / seed 可复现 fixture、Parquet 摘要和 smoke 数值；不同平台或库版本不承诺逐位一致。
随机运行 ID 用于防止覆盖，不作为模型随机种子。Qlib 以单 kernel 本地初始化，未启动 MLflow 服务。

Windows 中文目录已验证：LightGBM 原生 `save_model(path)` 无法写入当前路径，改用
`model_to_string()` 加 Python UTF-8 写文件；PyTorch checkpoint 通过 Python 打开的二进制句柄保存。

## 服务器与 GPU

运行前检查实际挂载、空间、inode / 配额、CPU / 内存 / 进程、GPU 使用情况及预约依据。
本轮设备枚举仅证明能查询八个 UUID；不能证明 GPU 已分配、八卡独立训练验收完成或允许抢占。
主代理尚未确认预约规则时禁止 GPU smoke，也不把空闲瞬间等同于无预约。

已安装兼容环境后，`ashare-lab devices` 只读枚举。GPU 执行接口要求完整 UUID 与资源确认：

```bash
# 仅在主代理已确认资源归属且重新检查空间 / 当前任务后使用；本轮并未据此运行。
.venv/bin/ashare-lab devices
.venv/bin/ashare-lab smoke --gpu GPU-<实际完整UUID> --allocation-confirmed
```

入口在导入 PyTorch 前设置 `CUDA_VISIBLE_DEVICES`，拒绝计算利用率非零、超过 64 MiB 已用显存
或存在计算进程的设备，再要求仅有一张可用 CUDA 卡。这个即时检查不是跨项目预约锁，
`--allocation-confirmed` 只能表明外部资源归属已被确认，不能用于绕过主代理的暂停决定。

服务器直连 PyPI / GitHub / PyTorch 源本轮发生 TLS 连接重置；不要把连接失败误报成包不存在。
必要时在本地下载校验过的轮子和 Python，再通过 SSH 传输，不带凭证、不借用旧项目环境。
下载前计算压缩包、解压环境、缓存与余量的峰值。当前 CUDA 环境待容量及资源条件确认，未传输大型包。
服务器执行源码必须先提交，再通过真实 origin 或 Git bundle 同步并核对 SHA；不在服务器临时改代码。

## 未完成验收

本地 synthetic 工程通过不代表 Phase 0 整体完成。真实数据读取、历史成员与状态覆盖、
正式复权 / 标签口径、冻结时间划分，以及获分配单卡训练另行验收。
数据源公开能力与限制见 [DATA_SOURCE_DECISION.md](DATA_SOURCE_DECISION.md)。

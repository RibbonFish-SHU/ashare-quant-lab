# 社区历史包离线导入主审

主代理，2026-10-04。社区导入器已通过候选级验收并集成到 main；正式研究资格仍为 false。
原实现为 `379f18235869e6301ea4f0ac3cd6d528d994587b`，开发基线为
`48cc074386fc87c75b293a6982121a51d21b58a0`。本轮发现并修复真实归档兼容问题，
实际成功构建使用干净提交 `e821b8eb1b69abb5b7fe6f2d635ec421331af712`，
集成提交为 `065476149accc012a75d789966fa8a19b363e273`。

完整记录见 [主审证据](evidence/phase1_community_import_main_acceptance.json)。

## 验收结果

- 构建范围：2023 年、原计划 336 个代码、242 个开市日，共 81,312 行；无重复证券/日期。
- 81,279 行价格候选、17 行有既存停牌参考的缺值、16 行 601059 上市前空位；未前向填充。
- 与旧候选重叠 81,296 行：325,116 个 OHLC 分币一致，68 个值缺失，无不一致；
  最大未舍入价格差为 0.0001572704 元。腾讯有限参考另有 29 行 / 116 个 OHLC 一致。
- 行情 Parquet 为 49 列 / 34 个 row group，对照 Parquet 为 81,325 行 / 25 列。
  构建过程完成全部 Parquet 和 DuckDB 往返；主审另用 DuckDB 重算覆盖、缺值、唯一键和对照结果。
- 原始成员区间 14,398 条。2023 年与条件重建比较 215 日一致；六月 12 日、十二月 15 日不一致。
  七条未映射历史标识单列保留，未生成正式股票池。
- 所有行的历史发布时间、可用时间、来源交易状态、精确股数与金额仍未知；24,216 行没有
  原供应商交易状态参考。量额的试验换算最大差为约 108.29 股、911.08 元，不能当成无损量额。
- `research_eligible=false`、质量为 `unverified`、正式门禁为 `blocked`；
  canonical reader 实际拒绝，错误为 `unsupported or incomplete dataset manifest`。

## 真实归档失败与修复

第一次构建在 379f182 上约 8.8 秒退出 1。归档 `csi300.txt` 有七条 `SHT00018` 区间，
日期为 2005–2007 年，严格数字证券代码校验导致 `invalid instrument interval`。
这与本次 2023 年行情范围无重叠。诊断记录绑定原文、行号与成员摘要；未重下载、删除或改写归档。

主代理在独立 `review/community-import-fix` worktree 修复，原执行 worktree 保持干净的
379f182。原始成员参考可以保留安全的未映射标识，`symbol=null`，不猜测股票映射；
若与请求区间相交仍拒绝。`all.txt` / feature 的数字代码要求、重复、倒置和未来日期检查保留。
九项回归在修复前为 6 失败 / 3 通过，修复后完整 **334 passed in 17.16s**；Ruff 和格式检查通过。

第二次构建使用 e821b8e，约 28.02 秒完成（外部记录 29.07 秒），按候选契约退出 2。
因此真实构建总数为 **2 次：1 次失败、1 次成功**；不是两次成功构建，也未覆盖重跑。
两次分别核对 67 / 68 份受保护文件（第二次含第一次失败 manifest），前后摘要全部一致；
所有本轮子进程已退出。

## 构建前结论

已审阅七个社区导入模块、来源配置、候选契约、测试及启动命令。源码绑定和历史版本上界
两项修复已落实；本轮未发现阻止离线候选验收的新增问题。九份源码/配置/测试文件与五份
检查日志的字节数及 SHA-256 均与版本化检查索引一致。复用完整 325 项、主仓库 cwd 下
71 项测试以及 Ruff/格式检查结果，不重复模型、Qlib 或 GPU 兼容性实验。

`phase1_community_import_source_checks.json` 的 `base_commit` 是正确的开发基线；
`source_commit_pending=true` 是提交前记录，已补充实际源码提交和核对时间，
保留原检查时间、测试结果及当时真实构建次数为零的历史事实。

## 本轮验收所有权

当前工具进程未提供 `HERDR_ENV=1`，无法通过 Herdr 环境检查；本轮不使用 Herdr 命令，
不向任一代理发送输入，也不创建替代核心代理。主代理继续其审阅和集成职责，承担离线
候选验收及所发现问题的修复。执行分支源码、测试、原报告及其预留候选目录保持原样。

- 原实现：`.cache/worktrees/phase1-community-import`；修复源码：`.cache/worktrees/phase1-community-review`。
  两次分别显式绑定实际加载模块所在的 `--project-root`。
- 失败产物：`datasets/candidates/community-2023-main-review-original336`，保留失败 manifest。
- 成功输出：`datasets/candidates/community-2023-main-review-original336-v2`。
- 主审进程、日志和输入前后摘要：`artifacts/community-main-review/<运行时间>/`。
- 主审脚本：`.cache/phase1/main-review/run_community_acceptance.py`、`run_community_recovery.py`、
  `verify_community_acceptance.py`；摘要在版本化证据中。
- 本批真实构建已经结束；执行代理恢复时先读取本报告，**不再启动旧的拟执行命令**。

源码 worktree 必须干净且仍为上述提交，输出必须不存在；运行前后核对绑定输入、旧候选、
来源账本与限制文件。CLI 成功构建候选按契约退出 2；同时要求 manifest 为
`built_candidate`、质量未失败且正式数据读取器拒绝，不能只依据退出码判断成功。

## 研究边界

范围仍为原计划 336 个代码的 2023 年数据。保留 `research_eligible=false`、未知历史
可用时间、量额误差及成员调整延迟；本批完成不代表整个 Phase 1 或正式研究数据验收完成。
后续按 [历史状态证据任务书](PHASE1_STATE_EVIDENCE_PLAN.md) 推进已有免费证据的离线接入。
主代理已核对下一批 40 份本地输入的摘要，见 [输入预检](evidence/phase1_state_evidence_input_preflight.json)；
这不等于重新解析或状态覆盖认证。交接与归属见 [下一批交接](PHASE1_STATE_EVIDENCE_HANDOFF.md)。

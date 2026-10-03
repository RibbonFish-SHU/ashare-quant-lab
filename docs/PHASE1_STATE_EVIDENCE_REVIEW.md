# 历史状态证据候选主审

主审日期：2026-10-04（北京时间）。本轮审阅执行代理提交 `2be10c58983a172ea99e3e5f5f3eb8cdd87cc937`，
在独立 worktree `review/state-evidence` 上进行；执行代理的源码、配置、测试和输入选择没有被改写。

## 集成结论

本提交通过源码和候选级主审，可以合并到 `main`。生成的状态数据只能作为离线历史参考候选，
仍保持 `research_eligible=false`、质量 `unverified`，不能作为 PIT 训练、回测、交易或风控输入。

主审在干净的 review worktree 上执行真实构建 **1 次**。CLI 返回码为 **2**，耗时
**11.600687999976799 秒**；这符合候选构建的受限退出约定。manifest 同时满足
`status=built_candidate`、`quality_status=unverified`、`canonical_reader_rejected=true`，
所有输出仍标记不可研究。

构建子进程已经正常退出，输入保护文件前后摘要相同。审阅封装脚本在子进程完成之后写入
`protected-after.json` 时把 Windows `Path` 对象用作 JSON 键，先以 `TypeError` 退出；这没有影响
构建产物。主审没有重跑构建，而是使用只读后验补齐执行记录、输入摘要和验收证据，并修复脚本供后续使用。
该封装问题和修复状态都记录在 [版本化验收证据](evidence/phase1_state_evidence_main_acceptance.json) 中。

## 候选产物核验

候选位于 review worktree 的
`datasets/candidates/state-evidence-2023-main-review-original336-v1`，包含 272 条事件和
81,312 条日级参考。独立 DuckDB 查询得到 336 个证券、242 个开市日，日期范围为
2023-01-03 至 2023-12-29；Parquet 与 DuckDB 往返均通过。项目正式 `read_dataset` 按契约拒绝该
`real_candidate` 命名空间，错误为 `unsupported or incomplete dataset manifest`，这是候选门禁的预期结果。

事件和日级分类如下：

| 检查 | 结果 |
| --- | --- |
| 名称事件 / 停牌事件 / 复牌事件 | 270 / 1 / 1 |
| `no_selected_halt_reference` | 81,307 |
| `partial_day_halt_reference` | 1 |
| `full_day_halt_reference` | 4 |
| `available_time` 未知事件 | 272 / 272 |
| `can_trade` 为 null | 81,312 / 81,312 |
| `historical_asof_certified=false` | 81,312 / 81,312 |
| 认证 `certified_st` | 0 行 |

688065 的时段语义与输入公告一致：2023-06-15 为下午开盘起停牌，虽然当日有 353,426 股成交，
因此保留为半日参考；2023-06-16、06-19、06-20、06-21 为完整停牌日；2023-06-26 为上午开盘
复牌参考。复牌事件没有回填 `available_time`，不会在严格 as-of 视图中提前关闭停牌事件。
2023-019 的 2022 年落款冲突仍作为事件字段保留。

实际输入验证为 71 份文件、336 个证券、365 个自然日和 242 个开市日，事件冲突为零。
执行代理报告早期文字写成 70 份；最终以输入验证结果和真实构建 manifest 为准。深交所当前简称 C
列没有用于历史状态；`000166.SZ`、`000333.SZ`、`001289.SZ`、`001979.SZ`、`300498.SZ` 没有名称
事件，保持未知。

## 源码与范围检查

执行代理提交已记录 67 项状态模块测试、401 项完整测试、Ruff 和格式检查通过。主审复用了这些
版本化检查结果，没有重复模型兼容性、回测或 GPU 实验。构建没有联网请求、服务器操作或 GPU 占用，
也没有读取 2024–2025 行情。

候选的 `available_time` 全部未知，名称连续性、ST 认证、供应商交易状态和完整停复牌覆盖仍未认证；
“无选定停牌参考”不等于正常交易。当前输出只能支撑后续工程联调和证据追踪，正式研究数据门禁仍然关闭。

## 证据索引

- [源码交付报告](PHASE1_STATE_EVIDENCE_REPORT.md)：实现、输入契约、测试和实际构建补录。
- [状态证据契约](STATE_EVIDENCE_CONTRACT.md)：事件、知识时间、日级参考和正式门禁语义。
- [主审验收 JSON](evidence/phase1_state_evidence_main_acceptance.json)：argv、PID、耗时、摘要、独立查询、表哈希和限制。
- `artifacts/state-evidence-main-review/20261003T195906.400243Z/`：构建日志、执行记录和输入前后摘要（位于项目忽略产物目录）。


# 历史状态证据接入交接

主代理，2026-10-04。执行任务见 [PHASE1_STATE_EVIDENCE_PLAN.md](PHASE1_STATE_EVIDENCE_PLAN.md)。
本次交接文件已准备，尚未通过 Herdr 发送；当前工具进程没有 `HERDR_ENV=1`，不执行外部控制。

## 先确认现状

- 社区导入已集成到 main，集成提交 `065476149accc012a75d789966fa8a19b363e273`。
  成功运行源码为 `e821b8eb1b69abb5b7fe6f2d635ec421331af712`，完整 334 项测试通过。
- 本批真实构建已结束：一次失败、一次成功；原执行分支拟定的输出目录未创建，
  不再启动原拟执行命令，不重复社区构建或模型兼容性实验。
- 主审成功产物为 `datasets/candidates/community-2023-main-review-original336-v2`；
  未研究晋级。完整摘要与旧输入不变证据见 [主审](PHASE1_COMMUNITY_IMPORT_REVIEW.md)。
- 原执行 worktree `.cache/worktrees/phase1-community-import` 仍为干净的 379f182；
  主审修复 worktree `.cache/worktrees/phase1-community-review` 为干净的 e821b8e。
  旧 worktree、原始文件、失败记录、来源限制和日志全部保留。

## 执行代理恢复后的范围

1. 恢复真实 Herdr 运行上下文后，先显式核实 `ashare-main` / `ashare-experiment` 的 pane、session
   与项目目录。历史核验位置为 w8:p1 / w8:p2，本轮没有重新核验，不能直接假定仍有效。
   不依赖 current 或界面焦点，不操作其他项目代理。
2. 从已同步且包含上述集成提交的 main 新建独立 `phase1/state-evidence` worktree，记录实际 HEAD。
   执行代理拥有新源码、配置、离线测试、候选构建、进程和执行报告；主代理维护方案与主审。
3. 先读取三份版本化来源锚点和原件，按任务书核对提取结果。
   [输入预检](evidence/phase1_state_evidence_input_preflight.json) 已验证 40 份文件、共 7,401,362 字节；
   本预检只证明保存字节一致，不替代 XLSX 单元格 / PDF 正文核验。
4. 原计划 134 只深市证券有 129 只、270 条截至 2023 年的名称参考；没有历史名称事件的五只
   保持未知。当前简称 C 列不用于历史状态，ST 字符串诊断不得冒充正式风险警示区间。
5. 688065 的停牌 / 复牌分别建事件：2023-06-15 下午开盘起停牌、06-26 上午开盘复牌。
   06-15 有 353,426 股成交，不能当作全天停牌；四个完整停牌开市日仍独立列出。
   公告落款年份冲突和网页零点日期标签继续保留，未知历史可用时间不可填造。
6. 先交稳定源码、离线测试、显式运行命令及输入选择配置给主审。稳定后运行新状态候选并保留
   `research_eligible=false`；不要覆盖行情候选，也不要提前关闭严格 as-of 视图中的停牌事件。

本批无需新的供应商账户；只使用已有原件，不联网扩采、不重试 BaoStock、不读取 2024–2025 行情，
不运行模型、回测、GPU 或服务器任务。真正的阻碍或语义不清应连同证据向主代理报告。

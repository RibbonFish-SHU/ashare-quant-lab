# Phase 1 原始层与候选层契约

本文件描述执行实现，不更改总体数据选择与研究时序。Phase 0 canonical 契约不变。
真实原始数据和以下候选数据始终 `research_eligible=false`；没有历史可得性证明时，不能进入模型或回测。

## 来源、获取与重放

- 来源依赖以 `requirements/source-baostock.txt` 单独锁定 BaoStock 0.9.4 官方 wheel 摘要。
  CLI 从该 wheel 加载 SDK；不向已有研究环境安装或升级包。SDK 依赖的 pandas 来自既有锁。
- 官方访问规则为 <https://www.baostock.com/blacklist>：每日 API 不超过 50,000 次且不能并发连接。
  本实现更保守：同一 Git 仓库的 worktree 共用 OS 排他锁与持久账本，按 Asia/Shanghai 自然日
  限制 10,000 次 wire 调用；每次 SDK send（含登录及分页）间隔至少 0.25 秒。账本先计数再发送。
  只约束通过此项目 CLI 发出的调用；其他应用 / 其他项目仍需各自协调来源访问。
- 一次子进程持有一个匿名连接、一个明确查询；默认总超时 60 秒，socket 默认超时 10 秒。
  可显式设 `--batch-size 20` 复用解释器导入：每次仍只登录 / 查询 / 关闭一个连接，完成后才执行下一项。
  最多 20 项共用同一个 60 秒硬上限，不放宽单项上限；成功项逐项保存 receipt，批次错误立即停止。
  中途超时保留已完成项和当前部分结果，不把尚未执行的项记为空返回。重试只执行失败的单项。
  只对传输丢失 / 查询总超时最多重试一次。供应商非零码、登录拒绝、解析错误和本地预算耗尽
  立即终止批次，保留记录；不切换端点、不规避黑名单。父进程等待或终止自己的子进程后才继续。
  已实测的黑名单码 `10001011` 另写入共享 `source-restriction.json`，后续在线缺缓存请求在本地返回非通过，
  不再次接触服务；离线复用仍可继续。状态保留至实际服务恢复得到确认，不设自动解封 / 探测定时器。
- 原始层 `provider-raw-v1` 记录 SDK 解码后的全部字符串、字段、参数、错误、观察时间、来源 URL、
  SDK 版本、摘要；不是网络 wire 字节的副本。零行成功、失败、部分行、非法表结构分别记录。
  JSON 始终保留；合法表结构另经 Parquet 和 DuckDB 逐单元格核对。
- run 保存计划原文内容及文件摘要、代码提交 / dirty 状态 / 文件摘要、进程 PID 和每次执行参数。
  独立 attempt 不覆盖；恢复仅复用精确 API + 参数 + 来源 + SDK 匹配且校验通过的成功记录。
  失败历史留在 run 中；候选构建只选每个逻辑查询的成功记录，不将失败计成空返回。
- Phase 0 旧归档校验 capture 与 Parquet 摘要后复用。一个旧 followup capture 未自带 SDK 名称，
  依据已审阅的 Phase 0 脚本 / 报告显式记为 `sdk_basis=phase0_report_and_script`；其传输完整性记为
  `legacy_not_instrumented`，不伪称通过新采集器的逐次 send 检查。
- 允许查询的日期为 2010–2023，必须显式 ISO 日期。2023 计划只为当前开发批次；2022 年末成员 /
  日历只用于验收年初基准。日线固定 daily、未复权；不提供默认 today 或封存行情的入口。

## 候选 schema：baostock-candidate-v1

这是与 Phase 0 canonical 隔离的新命名空间 `real_candidate`。每行保留 query_id、raw 文件与摘要、
旧 capture query_name、从 0 开始的行号、完整原字段 / 字符串、SDK、观察时间及精度。
`historical_publish_time` 和 `available_time` 均为空，`time_basis=unknown_historical_vintage`。
`valid_candidate` 只表示字段转换成功，不表示数据已获研究资格。

| 候选表 | 转换与限制 |
| --- | --- |
| bars | 编码校验 A 股市场 / 前缀；量为整数股、金额为人民币元、turn 百分数除以 100；价格等用 Decimal(32,12)，超精度拒绝而非舍入。源 tradestatus / isST 保留为可空布尔，停牌零量和停牌缺量单独分类。价格存在不证明能成交。 |
| calendar | 保留全部自然日及交易标志，来源作用域为 `SSE_SZSE_provider_joint`，不凭该联合接口伪造两个独立交易所来源。 |
| membership_snapshots | 请求日期、供应商 updateDate、代码、观察名称分开；300 个唯一代码只验形状。updateDate 不作为公告日期。 |
| st_snapshots / suspension_snapshots | 保留查询与更新日期；空返回不能证明没有 ST / 停牌，周度名单不能证明完整每日变化。 |
| securities | ipoDate 转为日期；outDate 保留为 `source_out_date`，其是否等于“退市生效首个自然日”未证实。status 和名称是本次观察值，不回填历史。 |
| dividends | 分别保留预告、股东大会、预案公告、实施公告、登记、除权、现金支付、红股上市日期。税前现金数值、税后原文、每股送股 / 转增及分配文本独立保存。不能把最终实施条款设为预案日已知。 |
| factors | 保存 source_forward_factor、source_backward_cumulative_factor、source_adjust_factor_value；不推导 split_multiplier。相邻因子比值是一项核对，不证明完整供应商算法或历史 vintage。 |

空字符串与不存在字段在 typed 候选中均为 null，但完整 raw_values_json 保留二者区别。
非数值、非有限数、负值、溢出、无效日期、代码错配、主动交易必填值缺失等产生可定位 fail，
对应候选行仍保留且标记 invalid，不填零、不丢行。OHLC 顺序与停牌非零成交矛盾也失败。
一条查询可覆盖相同市场行的不同版本；查询内重复键失败，跨查询相同值可并存，共同字段矛盾失败。

## 成员事件与质量门槛

`configs/csi-events-2023.json` 记录人工核对的中证公告事件、原件摘要、发布日期及日级精度、
收市后生效日和下一交易日。它是选择性事件集合，不能声称完整覆盖全部临时调样。
公告只有日期时不写精确时间，也不自动生成“次日零点即准确发布时间”。

质量输出 `candidate-quality-v1` 每项区分 pass / fail / unverified，包含计数与原始行定位。
覆盖转换、重复 / 矛盾、日历范围、开市缺行、上市前行、outDate 边界、停牌、ST、股票池形状、
周度集合变化、公告进出集合、公司行为和历史发布时间 / 修订风险。上市 / 退市窗口只有候选覆盖意义。
没有数据证明的事项保持 unverified，缺失行情不解释为零收益或自动停牌。

构建结束必须通过 Parquet 往返、DuckDB 完整读回，以及 canonical `read_dataset` 拒绝读取。
CLI `build` 输出产物后返回 **2** 表示研究门槛被阻止；不能把该返回值当采集失败，也不能因工程读取成功
把质量状态升级。`collect` 仅在全部计划查询已有成功证据时返回 0，否则返回 2。

# Phase 0 数据契约与时序

版本 `phase0-v1`。实现位于 `src/ashare_lab/contracts.py`、`storage.py`、`temporal.py`。
这些接口验证输入结构与已声明的时间语义，不能证明供应商声明本身真实，也不替代真实样本验收。

## 通用字段

| 字段 | 类型 / 语义 |
| --- | --- |
| event_date | Arrow date32；Asia/Shanghai 的交易日、成员生效起日或公司行为生效日 |
| source / source_version | 非空来源标识及原始快照 / 修订版本；保留出处，不能用下载日期假装历史版本 |
| record_version | 同一自然键的正整数修订号，随可用时间严格递增；不以版本字符串排序 |
| publish_time | 信息发布时间，必填且带时区；未知时拒绝进入可研究快照 |
| available_time | 该条修订可被研究流程获得的时间，不早于 publish_time |
| ingested_at | 本系统实际采集时间，不早于 available_time；历史回填不能据此杜撰历史可用时间 |
| data_kind | `synthetic` 或 `real`，行、Parquet 元数据、manifest 和目录命名必须一致 |

输入 timestamp 必须带时区，Arrow/Parquet 统一存储 `timestamp[us, UTC]`；交易日保留上海日期。
示例 `2024-01-02 16:00+08:00` 存储为 `2024-01-02 08:00Z`。拒绝日期字符串、naive timestamp、
必填空值、无穷 / NaN、重复修订主键和可用时间倒序。同日完整日线发布时间不得早于 15:00 上海时间。
这个收盘约束只适用于本项目沪深日频契约；它不意味着供应商恰在 15:00 完成发布。

## 表结构

以下表均附带上述通用字段。自然键再加 `record_version` 为完整主键。

| 表 | 自然键 | 内容与约束 |
| --- | --- | --- |
| bars | source, symbol, event_date | 原始未复权 open/high/low/close，正数且 OHLC 一致；volume 股、amount 人民币元、turnover 小数比例，非负；不插入零收益或可成交占位 |
| calendar | source, exchange, event_date | SSE/SZSE、is_open、带时区的 open_time/close_time；休市两时间为空，开市需同一上海日期且开盘早于收盘 |
| membership | source, index_symbol, symbol, membership_id | event_date 为生效起点，effective_to 为可空排他终点；采用 `[起日, 终日)`，稳定 membership_id 让终止日期可以通过新修订公布 |
| actions | source, symbol, action_id | event_date 为除权 / 生效日，action_type 为 split/cash_dividend/adjustment，factor > 0，cash_per_share >= 0；保留原始来源和修订历史 |
| status | source, symbol, event_date | listed_date、可空 delisted_date、is_st、is_suspended、active/suspended/delisted 状态、可空 limit_up/limit_down；未知涨跌停价保留为空，不等同可成交 |

股票标识为六位代码加 `.SH` / `.SZ`。数据契约保留退市记录，未实现任何“只保留当前上市股票”的过滤。
`delisted_date` 定义为退市在 Asia/Shanghai 生效的首个自然日（含当日），不是最后交易日：
若 `event_date >= delisted_date`，状态必须为 `delisted`；`delisted` 状态也必须提供已生效的退市日期。
此前的 `active` / `suspended` 状态可以保留已公告的未来 `delisted_date`，但仍遵守来源与可用时间约束，
不能提前标为 `delisted`。未知退市日期可为空，只能配合未退市状态；供应商不同日期口径需由适配器明确转换。
当前 actions 是工程交换格式：split 的 factor 表示新股数 / 原股数，cash_per_share 按原股数计；
`adjustment` 的 factor 必须由供应商适配器额外给出可追溯定义。没有该定义的因子不能参与正式复权。
配股、复杂权利变化、行动排序等正式口径尚未实现，不能用这个小样例声称复权完整。

## 可用时点与持久化

`as_of` 和 `query_as_of` 都先过滤 `available_time <= cutoff`，再按自然键选最新 record_version。
必须保留先前修订；先取最新版本再按时间过滤会错误删除历史可见值。
`members_at` 先做上述知识时点选择，再按生效区间筛选；多来源 / 多指数或重叠成员区间会报错，
调用前应明确选择来源和指数。公告已知但尚未生效的成员变更不会提前改变股票池。

`require_feature_available` 拒绝未来可用时间和未来 event_date。未来交易日历可以提前公告并供执行时序使用，
不能把未来交易的实际 open/close 当作当日特征。公司行为可提前作为公告信息保存；实际生效处理与公告特征应分开。

数据集目录为 `<root>/<synthetic|real>/<data_version>/`，五张 Parquet 和 manifest 一起写入临时目录再更名。
已有版本不可覆盖；失败 staging 目录保留供诊断。manifest 记录表行数和 SHA-256；读取验证摘要、
完整表集合、schema、命名空间与数据内容。DuckDB 使用绑定参数读取、UTC、单线程和 256 MB 内存上限。
这是小规模完整验证入口；未来大数据接入需要分区、跨表覆盖 / 缺失检查和增量处理，当前不宣称已实现。

## 五日标签的小时间轴

这组日期来自明确标记的 synthetic 工作日日历，不是已获准的真实研究区间或交易所完整日历。

| 相对时点 | 合成日期 / 上海时间 | 工程用途 |
| --- | --- | --- |
| T 收盘 | 2024-01-02 15:00 | 原始日线事件结束 |
| 信号 | 2024-01-02 16:00 | 只允许截至这一时点可见的信息 |
| T+1 开盘 | 2024-01-03 09:30 | 标签假设入场端点，严格晚于信号 |
| T+6 开盘 | 2024-01-10 09:30 | 五个开盘到开盘交易间隔后的退出端点；周末不计数 |
| 标签可用 | 2024-01-10 15:10 | 此样例假设以日线源取得退出开盘价，采用较晚的真实数据可用时间 |

`next_open_window` 仅输出 signal/entry/exit/available 四个时点；不会计算股票收益或使用未来价格拟合。
先按 signal 时点选取可见日历修订，再选择 exchange，要求这些记录有且仅有一个 source，
最后才筛选 `is_open`。第二来源即使只包含休市日或与第一来源日期完全不重叠，也会拒绝；
未到可用时点的来源、其他交易所的来源不参与该唯一性判断。同一 source 的不同 source_version / 修订可正常使用。
未来日历必须已在 signal 时点公告，不能用自然日加五代替交易日索引。
标签成熟时间由实际退出价格、公司行为等所需输入中最晚的可用时间决定，调用方提供；
不允许早于退出时点。日线供应商无法盘中提供 open 时，不能把 09:30 自动当作标签已成熟。

后续正式标签的候选定义为：基于原始执行价和持有期已生效公司行为构造总收益，
减去 T 信号时刻已知且当日有效历史股票池的同期等权总收益，再在同一股票池横截面排序。
需要主代理在真实数据验收后冻结复权、成员可用性、退市 / 停牌缺失标签处理、参照组合与成熟时间；
本轮未冻结、未计算这一公式。缺价、缺公司行为或缺历史成员时，不能以零收益补齐。
`adjustment` 因子也不能把目前下载的全历史最新复权曲线倒灌成历史特征。

## 训练边界

`training_intervals` 使用半开训练信号区间，只接受 fit_time 时已成熟的标签，
并按实际 `[entry_time, exit_time]` 检查与评估标签的价格区间重叠（共享端点亦剔除）。
拟合必须不晚于首个评估信号。它不随机划分，不用固定删除“五天”代替边界检查。
`check_transform_fit` 检查预处理拟合日期非空且属于训练日期集合；正式预处理管道仍待后续基线阶段。

用户真实数据范围、Walk-Forward 和最终封存规则见主代理维护的
[DATA_SOURCE_DECISION.md](DATA_SOURCE_DECISION.md)；当前草案没有被本轮测试读取或使用。

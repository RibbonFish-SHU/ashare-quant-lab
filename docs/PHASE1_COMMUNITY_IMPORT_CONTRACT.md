# 社区历史包候选契约 v1

本入口仅离线读取已固定的 `chenditc/investment_data` 2023-12-31 归档。
输入配置 `configs/community-2023.json` 绑定 release / asset / tag、真实下载记录、原请求计划、
三个旧候选及审阅锚点的保存字节。发布方没有 manifest / digest；本地 SHA256 只绑定所见原件。
本批原 336 代码用于覆盖诊断，不等于认证的历史沪深 300 股票池。

```text
python -m ashare_lab.community.cli --archive <archive> --source configs/community-2023.json --input-root <main> --plan <original-plan> --start 2023-01-01 --end 2023-12-31 --project-root <execution-worktree> --output <new-output>
```

必须从干净的稳定源码提交运行；输出目录必须不存在。成功生成候选退出 2，表示研究门禁
继续阻止。错误保留已建立的失败 manifest 和部分产物；不得覆盖后重跑。入口没有联网、
安装、供应商请求或上游脚本执行路径。

`project_root` 必须是实际加载 `community/pipeline.py` 所属 Git worktree 根；检查基于解析
后的 `__file__` 与 `git -C <module directory> rev-parse --show-toplevel`，在调用
`code_identity` 前完成，不能用共享 `.git` 推断。即使 main cwd 是干净仓库，若 PYTHONPATH
指向其他 worktree，漏传或错传 `--project-root` 都被拒绝。全部 community 模块必须出现在
源码摘要清单，且当前文件摘要一致，并与 `git -C <execution root> show <commit>:<path>`
逐字节核对（仅允许 Windows CRLF / LF 等价）。manifest 同时记录工作文件和提交 blob SHA256。

## 归档与时间

- gzip CRC / EOF、tar 路径 / 类型 / 重复及大小上限均检查。拒绝链接、特殊成员、稀疏文件、
  PAX 扩展、目录穿越、大小写别名及文件/目录冲突；不向文件系统解压归档成员。
- 上限：70,000 成员、每成员 16 MiB、普通文件合计 600 MiB、解压流 800 MiB。
  所有 feature 检查 little-endian float32 首值为有限非负整数索引，长度是 4 字节整数倍，
  末位在有序唯一日历内。只解码原计划代码在明确 2023 区间内的值。
- 入口明确只支持锁定的 `2023-12-31` tag，伪早 tag 即使所有 metadata 一致也被拒绝；
  校验后的 tag 上界同时传给归档读取和成员诊断。日历及 instrument 结束时间不能晚于
  该版本；多个日历必须一致。
  `all.txt` 仅用于核对证券是否存在，不能替代 IPO 或每日交易状态。
- `all.txt` 和 feature 仍只接受规范数字证券代码。其他原始成员参考可含安全的未映射标识，
  保留原始代码、日期与行号，`symbol=null`，不猜测对应股票。CSI300 中任一未映射区间
  与请求日期重叠时拒绝构建；区间外的记录列入 `unmapped_source_intervals`，原文仍保留。
  本次原包含七条 2005–2007 年的 `SHT00018`，不会作为 2023 年股票成员。
- 原始请求代码集合从计划查询重算并与 `codes` 精确比较；计划原件摘要必须匹配。
  候选覆盖计划代码 × 请求区间内归档日历，包括上市前空位和缺值，不前向填充。

## 行情字段与可追溯性

`community-quote-candidate-v1` 的 `quotes.parquet` 保存十个 `raw_*` float32 字段。
NaN 显示为 null，原始 IEEE 754 位模式仍在逐行 `feature_locations_json` 中，另区分
`nan` 和 `outside_member_span`。无限值、非正因子、非正 OHLC、负量额被拒绝。

- `restored_open/high/low/close` 是 float64 执行 `raw_price / raw_factor` 的结果，
  不冒充原始精确报价。原归一化 OHLC/factor 仍独立保留。
- `float32_input_error_bound_*` 给出输入 float32 半 ULP 误差传播的保守界，
  不包含上游归一化方法、复权口径或历史修订的不确定性。
- `experimental_volume_shares = raw_volume * raw_factor * 100`；
  `experimental_amount_cny = raw_amount * 1000` 只用于差异观察。来源量额单位仍未知，
  正式 `volume_shares/amount_cny` 保持 null，精确性标记为 false。
- 每行保存归档摘要、calendar 索引、每个 feature 的成员名/摘要、值索引、字节偏移及位模式。
  成员字节数和起始索引另存 `members.json`；日历副本与摘要由 manifest 绑定。
- 实际观察时间取已核实下载完成时间。发布版日期、下载时间均不写成 2023 年逐行历史发布
  或可用时间；`historical_publish_time/available_time` 保持 null。

## 质量与参考

来源独立保留：旧 BaoStock / 东方财富候选、经原件校验的深交所 IPO 参考、腾讯有限价格
参考均有原件和表摘要定位。重复/冲突版本、同源误用、调整口径或区间矛盾被拒绝。

上市日前有价格会报告失败；上市当日合法。缺值分为 `before_ipo_reference`、
`missing_with_suspension_reference`、`missing_reason_unknown`；停牌来源参考并不变成社区
数据自身的状态声明。未知退市、历史可用性及可成交性仍为 unverified。

`comparisons.parquet` 保留每个重叠位置的恢复价格、参考报价文字、浮点差和分币比较。
比较使用 `Decimal.from_float(recovered).quantize(0.01, ROUND_HALF_EVEN)`，每股/字段分别统计。
只有当前输入与参考实际证券/日期重叠、双方请求范围内的记录才参与；未知量单位不晋升。
匹配不能证明不同供应商具有独立上游。

`csi300.reference.txt` 为原始区间原文；`membership-reference.json` 只将其与已保存的
条件事件重建作诊断，明确输出假设和绑定的公告参考，重算年初集合及六月/十二月不一致日数。
不输出公告修正后的正式股票池，不默认两个定期事件就是全年全部事件，不从 factor 跳变
推导公司行为。全部候选、质量及成员诊断均为 `research_eligible=false`。

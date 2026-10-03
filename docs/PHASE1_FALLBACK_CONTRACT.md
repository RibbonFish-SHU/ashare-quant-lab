# 补充来源候选行情与采集约束

执行代理，2026-10-03。本实现对应主代理 main 中 `PHASE1_SOURCE_FALLBACK_PLAN.md`，
不改变原 BaoStock 数据、限制状态或历史缺失计数。候选 schema 为
`public-bars-candidate-v1`，正式数据读取器拒绝读取，`research_eligible=false`。

## 原始响应与身份

新采集的 `metadata.json` 必须同时满足 HTTP 200、`classification=complete`、
`transfer_complete=true`，且无非空 `error` / `exception`。响应字节数、SHA256、
若存在的 Content-Length、请求及最终 URL、参数、证券身份、供应商整数成功码均校验。
JSON 重复键 / 非有限常量、错误码、HTML/challenge、未识别数据、非预期行结构均拒绝；
只解析 JSON / 固定 JSONP 赋值，不执行响应代码。已识别证券的空数组与错误空对象分开。

`load_collection` 和采集恢复对每个成功 entry 重新读取权威 raw；entry 标记、
HTTP 200 或有效 body 都不能晋升失败 / 运行中元数据。worker 自报完成但证据矛盾时，
父进程记录失败并停止，不把进程返回码当作采集成功。

旧七份 probe 缺少 completion flags，只能经显式兼容入口读取。
`fallback/compatibility.py` 固定 main `bd83d73` 已核对的七组 metadata SHA256 → body SHA256；
另核对 probe manifest、原始 Parquet 摘要及每条捕获的身份 / 行数。
未知旧 raw 不能享受兼容例外，原件不改写。

## 计划、恢复与联网范围

`plan` 通过既有来源完整性检查读取 BaoStock 证券 run 和保留原计划，重算缺失日线，
按代码字典序选最多十只，不按价格或表现筛选。计划保存原 run / 原计划摘要，
采集和读取时重新验证选择与边界。run 保存独立原计划文件及摘要，不能通过删改嵌入计划
隐藏未完成请求。新来源自己的成功 / 缺失表不抵扣 BaoStock 的 401 个历史未完成请求。

唯一联网实现为已核对东方财富固定端点，日频 `klt=101`、未复权 `fqt=0`，
请求全为 2023-01-01 至 2023-12-31。腾讯仅复用离线 probe。不会调用包装器的当前行情
或后续年份默认查询。本批不发出 BaoStock 请求。

采集串行，相邻启动至少 1 秒；socket 超时 15 秒、单 worker 30 秒、body 上限 2 MiB。
仅传输错误 / worker 超时最多重试一次；限制 / challenge、供应商错误、协议异常立即停止，
写入东方财富独立持久状态，下一次运行也不能自动越过该状态。不跟随重定向或换端点。
同一 output 恢复时复用核验后的成功 raw；失败及部分响应、日志保留在独立 attempt 目录。

同项目 worktree 共享 `.cache/phase1/eastmoney-source-access/` 中的独立 OS 锁和账本；
BaoStock 的 `source-access/` 不变。只有本执行代理拥有本批采集进程。

## 单位、精度及时间

- 每行保留原始字段 JSON、原始量额文本、原件路径 / 摘要和行号；保留字面小数指数，
  不将小数位数解释为实际精度或舍入规则。
- 东方财富量原值为手、金额为元；乘 100 得到显示值对应的股数，`volume_is_exact_shares=false`。
  腾讯 688065 样本量为股，600000 / 000001 样本量为手，其他证券单位未知；
  腾讯金额为万元显示值。单位依据记录到每行，不静默归一为精确成交。
- Decimal 缩放在足够精度下执行；超过 schema 范围时保留 raw 并标为 invalid，不截断或四舍五入。
- 返回的当前名称 / 当前报价仅存在于 raw，不能作为历史特征。未知前收、ST、交易状态、
  历史发布时间及可用时间保持空，不从涨跌幅反推交易前收。
- 腾讯已观察到向前超出 start 的行：保留但不计入请求覆盖或比较；东方财富越界或腾讯超过
  end 均报边界失败。日期参数不允许 2024–2025。

## 质量状态

结构 / 数值错误、重复、乱序、已知闭市日出现行情、已知上市 / 首个退市日边界矛盾报 fail。
缺失日历参考不能被当作闭市；缺行仅为无法解释的缺失，不能推断停牌或截断。
只有 basic 行但 IPO / outDate 或退市语义未知时，上市信息及边界检查为 unverified。
已明确首个退市日时，当日及以后不属于可交易上市区间。

跨源核对输出逐字段差值及双方 raw 定位、实际比较行 / 字段数、无完整比较的行数和请求。
没有重叠、只有部分行 / 字段重叠时，即使差异数为 0，也不能标通过；仅当前候选区间内
所有行的六个数值都有独立比较且一致时，该项为 pass，仍不代表 PIT 或执行资格通过。
显示量额差异不被容差静默吞掉，保留 unverified。历史版本、执行信息和量额精度始终待验收。

## CLI

在本分支工作区使用现有 Python 3.11.16 环境，设置 `PYTHONPATH=src`：

```text
python -m ashare_lab.fallback.cli probe --source-root <main> --reference <既有候选目录> --output <新目录>
python -m ashare_lab.fallback.cli plan --baostock-run <原run> --original-plan <保留原计划> --output <新计划> --limit 10
python -m ashare_lab.fallback.cli collect --plan <新计划> --output <独立raw目录>
python -m ashare_lab.fallback.cli build --run <独立raw目录/run.json> --reference <既有候选目录> --output <新目录>
```

`probe` / `build` 保存完整 Parquet / DuckDB 往返结果、manifest、quality 和转换问题，
完成后退出 2 表示正式研究门禁仍阻止；不把此退出码误写成采集或存储失败。
`collect` 仅请求全部完成时退出 0；输出已有快照不能覆盖。

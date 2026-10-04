# 日期粒度公告的两个独立诊断视图

`ashare_lab.publication_timing` 只诊断给定证据在某个知识时点是否满足显式时间条件。
它不加载真实候选、不接入旧读取器、不访问文件或网络、不读取系统时钟或交易日历。
所有输出固定 `available_time=null`、`research_eligible=false`、`diagnostic_only=true`。
诊断“可见”不等于研究数据认证。

## 输入

`PublicationEvidence` 保存下列原值：

| 字段 | 含义和校验 |
| --- | --- |
| `evidence_id` | 非空且无首尾空白的证据标识 |
| `source_sha256` | 对应原件字节的64位小写 SHA-256；模块只校验格式，不替调用方读取原件验真 |
| `publication_date` | 来源日期标签的上海日期，`date` 或 `None` |
| `observed_at` | 实际抓取完成时刻，带时区的 `datetime` 或 `None` |
| `source_version_date` | 来源明确给出的版本日期，或下述显式附件创建日期情景下界，`date` 或 `None` |
| `source_version_date_basis` | 有版本日期时必须明确声明依据；无日期时必须为空 |
| `document_date` | 原件落款日，`date` 或 `None` |
| `date_conflict` | 显式矛盾标记，严格布尔值，默认 `False` |
| `publication_label_kind` | `date_only` 或 `minute_label_unverified`，默认前者；均只作日期标签解释 |

日期字段不接受字符串、`datetime`、epoch 数字或布尔值。调用方可先用
`date.fromisoformat(...)` 严格解析已核实的日期标签。带分钟但未核实的 API 标签只能先取上海日期，
用 `minute_label_unverified` 保留其性质，不能直接塞进观察时间冒充精确发布时间。
观察时刻必须来自实际抓取完成记录；文件 mtime、下载开始时刻或当前系统时钟都不是替代输入。

允许的版本日期依据只有：

- `source_declared_version_date`：调用方从来源明确版本声明中读出的日期；本模块未认证整个修订链。
- `source_reported_attachment_created_date_assumption`：来源所报附件创建日期，仅作为
  **情景下界假设**。输出明确声明这不是历史版本发布日期，不把 `createAt` 的语义自动升级。

文件系统 mtime、PDF 制作时间、文件名时间、事件生效日期或观察时刻等依据不被接受。
调用方仍须核验原件和字段来源；用一个正确的依据字符串不能证明提交的日期真实。

## 查询与输出

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo
from ashare_lab.publication_timing import PublicationEvidence, diagnose

evidence = PublicationEvidence(
    evidence_id="synthetic-example",
    source_sha256="a" * 64,  # 仅示例；实际调用必须提供对应原件摘要。
    publication_date=date(2023, 6, 8),
    document_date=date(2023, 6, 7),
    observed_at=datetime(2026, 10, 4, 16, tzinfo=ZoneInfo("Asia/Shanghai")),
)
result = diagnose(
    evidence,
    mode="date_upper_bound",
    knowledge_time=datetime(2023, 6, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
)
```

每次查询必须选一个模式，知识时点必须带时区。所有比较归一到 UTC，使用包含端点的 `<=`，
不依赖运行电脑时区。输出保留输入时区表达及规范化 UTC 时刻。

| 模式 | 规则 | 专属输出 |
| --- | --- | --- |
| `date_upper_bound` | `max(公告日期, 已知来源版本/附件情景下界日期)` 后一上海自然日零点 | `scenario_basis_date`、`scenario_assumed_at_utc` |
| `observed_at` | 实际抓取完成时刻本身；此后才满足观察事实视图 | `observed_visible_at_utc` |

两种模式不互相补值。日期视图中 `observed_visible_at_utc` 为空，观察视图中
`scenario_assumed_at_utc` 和 `scenario_basis_date` 为空。`normalized_observed_at_utc` 只是
保留输入观察事实的上下文，不能代替所选视图的专属时点。

`status` 为 `visible`、`not_yet_visible` 或 `unavailable`。前两者分别对应
`visible_by_knowledge_time=true/false`；无法建立所选时点时该值为 `null`，并提供
`blocked_reasons`，不把“未知”伪装为已经判定的不可见。

日期视图只是条件性的保守情景，不是经过认证的绝对历史上界。输出始终带有
`historical_revision_history_verified=false` 及未核实的历史修订风险；即使某个版本日期已知，
也不能证明这些原件字节在当时确实公开。当前捕获视图同样不能逆推出历史发布时间。

## 矛盾、未知与边界

- 落款日早于公告日期是合法的隔夜披露。相等也合法；都使用公告日期，不前移到落款日。
- 落款晚于公告日期、显式 `date_conflict=true`，或任一已知日期晚于实际观察的上海日期，
  阻止日期假设视图。即使落款早于公告日期，显式矛盾标记也不能被忽略。
- 没有公告日期，不以版本日、落款日或观察日补齐日期假设。
  没有观察时刻，也不从任何日期或系统时钟构造观察视图。
- 日期矛盾保留在 `date_conflicts`；它们不抹去实际已抓到原件的观察事实。
  因此观察模式可在抓取完成后返回可见，同时保留矛盾和研究资格限制。
- 已知晚版本或附件日期下界会推迟日期情景，不回填为原公告日可见。
  已知边界日期更早时不能使情景早于公告日期。
- 使用自然日，不自动跳周末、节假日。策略信号须由调用方另接显式交易日历。
  同日开盘生效的复牌公告，如果只有当天日期标签，次日零点假设依然晚于该次开盘，不能“补救”前移。
- 超出 Python 日期范围的次日上界返回不可用原因，不绕回早期日期。

## 验证与归属

相关检查为 `tests/test_publication_timing.py` 和新模块的 Ruff 检查，无需模型、GPU 或 smoke。
独立 worktree 复用主仓库解释器时必须显式设置 `PYTHONPATH` 指向本 worktree 的 `src`。
本模块不修改已有候选、源文件、门禁或正式划分；真实证据案例由主代理独立审阅。

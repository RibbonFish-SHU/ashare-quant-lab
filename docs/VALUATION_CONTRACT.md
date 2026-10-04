# 含公司行为权益的合成收盘估值

`ashare_lab.valuation.value_at_close` 读取现有合成 `ActionState`，输出收盘资产及逐证券分解。
接口不推进账本阶段，不下单，不把结果升级成正式回测或真实受限股公允价值。

## 输入与时间边界

```python
from ashare_lab.valuation import SAME_UNADJUSTED_CLOSE, ValuationPrice, value_at_close

result = value_at_close(
    state,
    {
        "000001.SZ": ValuationPrice(
            symbol="000001.SZ",
            event_date=trading_date,
            close_price="12.00",
            available_time=close_known_at,
            adjustment_basis="unadjusted",
            data_kind="synthetic",
        )
    },
    on_date=trading_date,
    asof=close_asof,
    state_processed_through=close_asof,
    corporate_action_scope_complete=True,
    uncredited_share_policy=SAME_UNADJUSTED_CLOSE,
)
```

- `on_date` 必须是显式 `date`，并存在于状态的交易日历中。
- `asof` 必须带时区；上海日期须等于 `on_date`，当地时刻须不早于 15:00。
  等价的 UTC、上海等带时区时间会归一成 UTC。
- `state_processed_through` 必须显式提供，归一后与 `asof` 相同。
  这是调用方对本合成场景已处理至查询时点的声明，**不能证明历史数据完整性**。
  `state.clock` 是最后一次实际账本操作时间，只要求不晚于 `asof`；无新事件日无需伪造阶段。
- `corporate_action_scope_complete` 默认未知，必须显式为布尔 `True`。已登记且除权日已到、
  但没有 `ex_accrual` 的事件仍被拒绝，包括零权益事件，不能用声明绕过已知漏记。
- 输入状态经公开 `export_state` / `restore_state` 检查摘要、阶段与账户链后形成独立副本。
  摘要是内容完整性绑定，不是历史完备性证明或外部认证。

## 价格与股份假设

每个持有非零已到账股数或非零待到账股数的证券，需要本交易日的正值未复权收盘价。
已到账且仍被 T+1、上市日期或显式可卖日期锁定的股份也需要价格并计入市值。
接口不拿成本价、昨日价格或零值补缺价。

价格字典必须由证券代码映射到 `ValuationPrice`；所有传入价格均检查证券一致、同日和可见性。
`available_time` 不得早于该价格日的上海收盘，也不得晚于查询时点。
`adjustment_basis` 仅接受显式 `"unadjusted"`；未知、前复权、后复权等口径被拒绝，
避免复权价格与单列公司行为权益发生重复补偿。价格只接受精确 `Decimal`、字符串或整数，
不接受浮点、布尔、零、负数、NaN 或无穷值。

存在非零待到账普通股时，必须显式选择
`SAME_UNADJUSTED_CLOSE = "same_security_unadjusted_close"`：按同证券未复权收盘价估值。
这是明确的合成假设，不证明真实受限股公允价值；缺价仍失败。没有待到账股时可以传 `None`。
未知假设始终失败，不因当前余额为零而默许拼写错误。

## 资产公式与生命周期

```text
NAV = 可用现金 + 已到账股份市值 + 税前现金应收
      - 合成代扣待付 + 待到账股份假设市值
```

登记至除息前不额外计入应收或待到账股。`ex_accrual` 后才计入；现金实际支付后，
应收减为零且现金增加税后金额；股份实际到账后，待到账股减少且已到账股增加。
计划支付/到账日期本身不代表已经收到，延迟到账继续保留对应应收。
原股份在登记后已卖出也不抹去登记权益。仅持现金应收的证券及零股条目无需行情。

数量乘价、逐证券汇总和 NAV 均用 `Decimal`，估值层不另外按证券或资产类别舍入到分。
现金、税前应收和合成代扣金额沿用账本原有的分位舍入结果；估值层不重算税。
因此相同价格下待到账股转为已到账股不会因二次分组舍入增加或减少资产。

整个状态恢复、权益核对、逐证券乘价及全账户求和都在独立 Decimal 上下文中进行，
不继承调用方的精度、陷阱或标志，也不修改调用方上下文。为使充分精度有明确边界，
状态与价格中的每个 Decimal 系数最多 128 位、指数限于 -128 至 128；整数绝对值须小于
`10**128`。超界明确拒绝，不悄悄舍入。局部精度至少 544 位，并按输入数字数量增加求和进位空间。
现有账本计算现金及合成代扣时的分位舍入仍按其原规则执行。

例如：100 股、收盘价 20 元，每股税前分红 2 元、送股 0.5 股，理论除权价为 12 元。
除权后原股价值 1200、待到账股价值 600、税前应收 200，共计 2000；若明确采用合成 10%
代扣，则扣除待付 20，NAV 为 1980。支付及股份到账只转换资产类别，NAV 仍为 1980。

## 输出与对账

返回独立字典，金额保留 `Decimal`，日期和时间保留 `date` / UTC `datetime`；它不是 JSON 导出器。
修改返回字典不会修改输入状态、行情或声明字典。

| 字段 | 含义 |
| --- | --- |
| `account_id`, `state_integrity`, `state_clock` | 账户与本次读取状态的内容绑定 |
| `on_date`, `asof`, `state_processed_through` | 交易日、知识时点及调用方处理完成声明 |
| `corporate_action_scope_complete`, `uncredited_share_policy` | 合成范围与待到账股假设 |
| `price_adjustment_basis` | 固定为 `unadjusted` |
| `spendable_cash` | 账户可用现金 |
| `credited_shares_market_value` | 所有已到账股份市值，包含锁定股份 |
| `gross_cash_receivable`, `withholding_payable`, `net_cash_receivable` | 税前应收、合成代扣待付及两者之差 |
| `uncredited_shares_market_value` | 显式假设下的未到账股份市值 |
| `nav` | 以上公式的资产总额 |
| `securities` | 按证券代码排序的独立分解，可逐项求和与总额对账 |

每个证券行包含：已到账股数 `credited_shares`、按日期已解除账本锁定的股数
`date_available_shares`、仍锁定股数 `locked_credited_shares`、未到账股数 `uncredited_shares`、
实际使用价格及其日期/可见时间/口径，和相应市值、应收、代扣及
`value_excluding_spendable_cash`。无股数需要估值时 `price=None`。
`date_available_shares` 只说明账本日期锁定，不保证市场开市、流动性、涨跌停或实际可成交。
现金属于账户，不强行摊给证券；证券价值之和加 `spendable_cash` 等于 `nav`。
逐证券现金权益和待到账股汇总还会与公开 `cash_claims` 结果复核。

所有结果固定为 `data_kind="synthetic"`、`research_eligible=False`，
仅用 `supported_synthetic_scope_valued=True` 表示本接口支持的合成范围已估值。
`complete_portfolio_valuation_available=False` 保持保守含义；旧账本字段、导出结构和执行接口不变。
本层不新增真实数据适配、历史无泄漏认证、差别化持股期税制或正式策略结论。

## 验证范围

`tests/test_valuation.py` 使用真实账本生命周期构造输入，覆盖登记、除息、延迟到账、
股份锁定和释放、登记后卖出、仅现金应收、零权益、多证券/多事件汇总、重放及保存恢复。
手算样例解释税差及资产守恒；边界测试覆盖未知范围、漏计提、缺价、未来价/状态、错误证券、
复权价、非法数值和时间，以及成功/失败路径输入不变。本批不运行模型或正式回测。

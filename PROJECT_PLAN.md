# A-Share Quant Lab

> 基于 8×NVIDIA RTX A5000 的个人 A 股量化研究、训练、回测与实盘交易系统。

## 1. 项目目标

本项目旨在搭建一套完整、可复现、可扩展的个人 A 股量化研究与交易平台。

核心原则：

- **8 张 GPU 用于研究和训练，而不是长期实盘推理**
- **研究端与交易端彻底解耦**
- **先建立可信基线，再逐步引入前沿模型**
- **优先解决数据泄漏、回测失真、交易约束和过拟合问题**
- **Agent 只负责研究，不直接控制实盘账户**
- **复杂模型必须证明相对简单基线具有稳定增量价值**
- **最终部署模型尽量控制在 CPU 或单张 A5000 可完成推理的规模**

第一阶段不以“预测某只股票明天具体价格”为目标，而是解决一个更适合量化的问题：

> 在给定股票池中，对股票未来一段时间的超额收益进行横截面排序，并据此构建可执行的组合。

---

# 2. 第一版策略定义

## 2.1 股票池

初始研究股票池：

- 沪深 300 历史成分股

后续扩展：

- 中证 500
- 中证 1000
- 全 A 股可交易股票池

必须使用**历史时点真实成分股**，禁止用当前指数成分反推历史，否则会产生幸存者偏差。

---

## 2.2 交易方向

第一版：

- 只做多
- 不加杠杆
- 不融券
- 不做高频
- 不追求盘口级预测

---

## 2.3 信号频率

第一版采用：

- 日频特征
- 每日收盘后生成信号
- 初始调仓周期：5 个交易日
- 实际下单：下一交易日执行

---

## 2.4 预测目标

不预测：

```text
明天收盘价是多少？
```

而预测：

```text
未来 5 个交易日中，
当前股票相对于股票池内其他股票的收益排名。
```

第一版标签：

```text
未来 5 日超额收益
+
横截面 Rank
```

后续可扩展成多任务：

```text
1 日收益
5 日收益
10 日收益
未来波动率
未来最大回撤
上涨概率
横截面 Rank
```

---

# 3. 总体系统架构

```text
┌──────────────────────────────────────────────┐
│            Research / Training Server        │
│                8 × RTX A5000                 │
├──────────────────────────────────────────────┤
│                                              │
│ Historical Data                             │
│      │                                       │
│      ▼                                       │
│ Data Pipeline                               │
│      │                                       │
│      ▼                                       │
│ Feature / Factor Store                      │
│      │                                       │
│      ├──────────┬──────────┬────────────┐    │
│      │          │          │            │    │
│ LightGBM     MLP /       Time-Series   Agent │
│ Baseline     Transformer Foundation    Alpha │
│               Models       Model       Mining│
│      │          │          │            │    │
│      └──────────┴──────────┴────────────┘    │
│                     │                        │
│                     ▼                        │
│          Walk-Forward Validation             │
│                     │                        │
│                     ▼                        │
│            Backtest + Stress Test            │
│                     │                        │
│                     ▼                        │
│           Model / Signal Versioning          │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────┐
│               Inference Layer                │
├──────────────────────────────────────────────┤
│ Latest Data                                  │
│      │                                       │
│ Feature Calculation                          │
│      │                                       │
│ CPU / Single-GPU Inference                   │
│      │                                       │
│ Alpha Score                                  │
│      │                                       │
│ Portfolio Construction                       │
│      │                                       │
│ Target Position File                         │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────┐
│               Trading Layer                  │
├──────────────────────────────────────────────┤
│ Risk Check                                   │
│      │                                       │
│ Position / Cash Check                        │
│      │                                       │
│ Broker Adapter                               │
│      │                                       │
│ QMT / miniQMT / Other Broker API             │
│      │                                       │
│ Orders                                       │
│      │                                       │
│ Execution Report                             │
│      │                                       │
│ Position Reconciliation                      │
└──────────────────────────────────────────────┘
```

---

# 4. 硬件规划

当前硬件：

```text
8 × NVIDIA RTX A5000
24 GB VRAM / GPU
192 GB aggregate VRAM
```

8 张卡主要用于：

- 多模型并行训练
- 多时间窗口并行验证
- 超参数搜索
- 多随机种子实验
- 时序基础模型训练 / 微调
- 自动因子研究
- Ensemble 训练
- 文本 / 公告 Embedding 批量生成

---

## 4.1 日常研究模式

不建议长期把 8 张卡全部用于一个模型。

更适合：

```text
GPU 0 -> Experiment A
GPU 1 -> Experiment B
GPU 2 -> Experiment C
GPU 3 -> Experiment D
GPU 4 -> Experiment E
GPU 5 -> Experiment F
GPU 6 -> Experiment G
GPU 7 -> Experiment H
```

例如：

```text
2 models
×
4 walk-forward windows
=
8 GPU tasks
```

---

## 4.2 大模型训练模式

需要训练较大的时序模型时：

```text
2 GPU
4 GPU
8 GPU
```

按实际扩展效率使用：

- PyTorch DDP
- FSDP
- DeepSpeed

不能为了“用满显卡”而强制多卡。

---

## 4.3 实盘推理

实盘目标：

```text
优先 CPU
      ↓
不够再使用
      ↓
1 × A5000
```

原则：

> 训练系统可以很复杂，部署系统必须尽量简单。

研究模型如果非常复杂，但有效，可以考虑：

```text
Teacher Ensemble
        │
        ▼
Student Model
        │
        ▼
Lightweight Inference
```

但只有在确有运行压力时才做蒸馏。

---

# 5. 推荐技术栈

## 5.1 基础语言

```text
Python
```

主要版本统一并锁定依赖。

---

## 5.2 研究框架

推荐：

```text
Microsoft Qlib
```

作用：

- 数据处理
- 特征工程
- 模型实验
- 回测
- 统一实验流程

---

## 5.3 深度学习

```text
PyTorch
```

用于：

- MLP
- Transformer
- Temporal Model
- Graph Model
- Foundation Model
- Distillation

---

## 5.4 传统模型

```text
LightGBM
```

作为必须长期保留的基线。

任何复杂模型都必须和 LightGBM 在相同数据、相同股票池、相同交易成本下比较。

---

## 5.5 数据存储

第一版：

```text
Parquet
+
DuckDB
```

优点：

- 简单
- 快
- 便于版本管理
- 方便 Python 查询
- 不需要过早搭建复杂数据库系统

以后数据量扩大后，可增加：

```text
ClickHouse
```

---

## 5.6 Agent 研究

推荐参考：

```text
Microsoft RD-Agent
```

用途：

- 自动提出因子假设
- 自动生成因子
- 自动测试
- 自动记录失败实验
- 自动提出下一轮候选

Agent 不允许：

- 接触券商账号密码
- 直接下单
- 修改交易风控
- 查看最终封存测试集后继续调参
- 随意修改回测交易成本

---

# 6. 数据体系

## 6.1 第一阶段数据

至少需要：

### 行情

```text
Open
High
Low
Close
Volume
Amount
Turnover
```

### 股票状态

```text
上市日期
退市日期
停牌
ST / *ST
涨停
跌停
交易状态
```

### 市场数据

```text
指数行情
行业指数
市场成交额
市场波动率
```

### 股票池

必须保存：

```text
历史指数成分变化
```

---

## 6.2 第二阶段

加入：

```text
财务数据
估值指标
机构持仓
资金流
公告
新闻
分析师预期
```

---

## 6.3 第三阶段

再加入：

```text
5min
1min
Level-2
逐笔成交
盘口
```

只有当日频系统已经验证完成，才考虑这一层。

---

# 7. 数据反泄漏体系

这是整个项目中最重要的模块之一。

所有数据尽量保存：

```text
event_date
publish_time
available_time
source
version
```

必须保证：

> 模型在交易日 T 时，只能看到交易日 T 当时真正已经公开的数据。

典型错误：

```text
财报属于 Q1
≠
3 月 31 日就能使用
```

正确逻辑：

```text
财报实际公告时间
↓
公告之后才能进入模型
```

---

# 8. 第一阶段模型体系

建立三层基线。

---

## 8.1 Baseline 0

最简单策略：

```text
等权组合
```

作用：

验证回测系统本身。

---

## 8.2 Baseline 1

```text
Alpha158 / Alpha360
+
LightGBM
```

这是核心基线。

输出：

```text
Alpha Score
```

然后横截面排序。

---

## 8.3 Baseline 2

```text
MLP
```

同样输入特征。

用于判断：

```text
神经网络是否比树模型产生真实增量。
```

---

# 9. 第二阶段深度模型

只先引入一个深度模型。

候选：

```text
MASTER
```

或：

```text
Custom Temporal Transformer
```

推荐输入：

```text
20 / 60 day feature sequence
+
Market Regime Features
```

输出：

```text
Cross-sectional Alpha Score
```

不要一开始同时部署：

- Transformer
- LSTM
- GRU
- TCN
- GNN
- MoE
- Foundation Model

否则无法判断性能来自哪里。

---

# 10. Time-Series Foundation Model

第二阶段后半部分加入。

候选：

```text
Kronos
TimesFM
Chronos
Moirai
```

实验必须拆成：

### A. Zero-shot

```text
Pretrained Model
↓
Direct Prediction
```

### B. Feature Extraction

```text
Pretrained Model
↓
Embedding
↓
LightGBM / MLP
```

### C. Fine-tuning

```text
Financial Data
↓
Fine-tune Foundation Model
```

### D. Domain Pretraining

最终可以尝试：

```text
AStockFM
```

即：

> 自己训练一个专门面向 A 股的时序基础模型。

数据可以包含：

```text
全 A 股
指数
行业
ETF
日线
5min
1min
```

---

# 11. 自动 Alpha 工厂

这是 8 卡服务器最值得投入的方向之一。

基本循环：

```text
Research Agent
      │
      ▼
Generate Hypothesis
      │
      ▼
Generate Factor
      │
      ▼
Static Check
      │
      ▼
Leakage Check
      │
      ▼
Backtest
      │
      ▼
Evaluate
      │
      ▼
Store Result
      │
      ▼
Generate Next Hypothesis
```

---

## 11.1 因子约束

Agent 初期只允许：

```text
Price
Volume
Amount
Turnover
Rolling Statistics
Rank
Correlation
Momentum
Volatility
```

生成公式。

不要初期允许任意 Python。

---

## 11.2 因子评价

至少包括：

```text
IC
Rank IC
ICIR
Turnover
Factor Correlation
Stability
Cost-adjusted Return
```

---

## 11.3 防止 Alpha Mining 过拟合

必须记录：

```text
所有尝试
```

而不是只留下成功因子。

同时统计：

```text
尝试了多少因子
最终留下多少
```

避免：

```text
试了 10000 个因子
找到 3 个很好
↓
误认为存在稳定 Alpha
```

---

# 12. 横截面模型设计

每天构建：

```text
股票 1 -> Features
股票 2 -> Features
股票 3 -> Features
...
股票 N -> Features
```

模型输出：

```text
Stock 1 -> Score
Stock 2 -> Score
...
Stock N -> Score
```

然后：

```text
Rank
↓
Neutralization
↓
Portfolio Construction
```

---

# 13. Multi-Task Learning

后续可升级为：

```text
Shared Encoder
    │
    ├── 1D Return
    ├── 5D Return
    ├── 10D Return
    ├── Volatility
    └── Drawdown Risk
```

Loss 示例：

```text
Return Loss
+
Rank Loss
+
IC Loss
+
Risk Loss
```

换手惩罚可以放在组合层，而不是盲目写进神经网络 Loss。

---

# 14. Graph Model

第三阶段再加入。

把股票表示为 Graph。

Node：

```text
Stock
```

Edge：

```text
Industry
Concept
Supply Chain
Ownership
Return Correlation
Fund Flow Correlation
News Co-occurrence
```

架构：

```text
Temporal Encoder
       │
       ▼
Stock Embedding
       │
       ▼
Graph Attention
       │
       ▼
Cross-sectional Score
```

必须单独测试：

```text
Without Graph
vs
With Graph
```

确认图关系确实提供增量。

---

# 15. LLM / 新闻 / 公告

LLM 不负责直接回答：

```text
明天股票涨不涨？
```

LLM 负责：

```text
公告 / 新闻
      │
      ▼
Structured Event Extraction
```

例如：

```json
{
  "event": "earnings_revision",
  "direction": "positive",
  "magnitude": 0.73,
  "surprise": 0.62,
  "certainty": 0.91,
  "horizon": "medium"
}
```

然后把它转成：

```text
Event Embedding
+
Text Embedding
```

输入量化模型。

---

# 16. Walk-Forward 验证

禁止随机拆分金融时间序列。

错误：

```text
2010-2025
Random 80 / 20 Split
```

正确：

```text
Train: 2010-2018
Valid: 2019
Test : 2020

Train: 2010-2019
Valid: 2020
Test : 2021

Train: 2010-2020
Valid: 2021
Test : 2022
```

可以采用：

```text
5 年训练
+
1 年验证
+
6 个月测试
```

窗口持续滚动。

---

# 17. Final Holdout

必须额外留一段：

```text
Final Blind Test
```

原则：

> 在模型、特征、参数确定之前不能查看。

一旦看了并根据结果改模型，这个测试集就已经失效。

---

# 18. 回测系统

回测必须模拟真实交易约束。

至少包括：

```text
T+1
停牌
涨停无法买入
跌停无法卖出
ST
退市
交易单位
手续费
印花税
滑点
现金不足
部分成交
未成交订单
```

---

# 19. 成本压力测试

至少运行：

```text
Normal Cost
Higher Cost
Extreme Cost
```

如果策略只在极低手续费下盈利：

```text
不进入实盘。
```

---

# 20. 模型评价体系

不能只看收益率。

---

## 20.1 Prediction Metrics

```text
IC
Rank IC
ICIR
Directional Accuracy
```

---

## 20.2 Trading Metrics

```text
Annualized Return
Excess Return
Sharpe
Sortino
Max Drawdown
Calmar
Turnover
Win Rate
```

---

## 20.3 Stability

按：

```text
年份
牛市
熊市
震荡市
高波动
低波动
行业
市值
```

拆分结果。

---

# 21. 模型晋级规则

新模型要进入下一阶段，必须同时满足：

## 可信

```text
无时间泄漏
无明显数据错误
回测规则完整
```

## 有增量

```text
相对于 LightGBM
在多个 Walk-Forward 窗口稳定改善
```

## 可实施

```text
扣成本后仍具有优势
推理速度可接受
交易限制可执行
```

---

# 22. Ensemble

只有单模型验证通过之后再做。

例如：

```text
LightGBM
   │
Transformer
   │
Foundation Embedding
   │
Agent Factors
   │
   ▼
Ensemble
```

组合方法：

```text
Rank Average
Weighted Average
Stacking
```

权重只能在训练 / 验证集确定。

---

# 23. 实盘信号格式

研究端输出：

```text
scores.parquet
```

字段：

```text
date
symbol
alpha_score
model_version
data_version
generated_at
```

然后生成：

```text
targets.json
```

例如：

```json
{
  "trade_date": "YYYY-MM-DD",
  "strategy": "ashare_quant_v1",
  "model_version": "v1.3.2",
  "positions": [
    {
      "symbol": "600000.SH",
      "target_weight": 0.05
    }
  ]
}
```

---

# 24. 风控系统

风险控制与模型完全分离。

Agent 和模型不能修改：

```text
Maximum Position
Maximum Industry Exposure
Maximum Total Exposure
Cash Reserve
Daily Turnover Limit
Order Size Limit
```

---

## 24.1 异常情况

以下任何情况：

```text
数据过期
数据缺失
账户持仓不一致
订单状态未知
券商连接异常
信号版本异常
模型版本未知
```

系统默认：

```text
停止新增风险
```

而不是继续盲目交易。

---

# 25. 交易接口

适配层保持抽象：

```text
BrokerAdapter
```

实现：

```text
QMTAdapter
MiniQMTAdapter
PaperTradingAdapter
```

接口：

```python
get_account()
get_positions()
get_orders()
place_order()
cancel_order()
```

研究代码绝对不能直接调用券商 API。

---

# 26. 推荐仓库结构

```text
ashare-quant-lab/
│
├── README.md
├── PROJECT_PLAN.md
├── pyproject.toml
├── requirements.txt
│
├── configs/
│   ├── data/
│   ├── models/
│   ├── backtest/
│   └── trading/
│
├── data_pipeline/
│   ├── download/
│   ├── clean/
│   ├── align/
│   └── validation/
│
├── datasets/
│   ├── raw/
│   ├── processed/
│   └── metadata/
│
├── features/
│   ├── alpha158/
│   ├── alpha360/
│   ├── custom/
│   └── agent_generated/
│
├── models/
│   ├── lightgbm/
│   ├── mlp/
│   ├── transformer/
│   ├── foundation/
│   └── graph/
│
├── research_agents/
│   ├── factor_agent/
│   ├── hypothesis_agent/
│   └── evaluator/
│
├── training/
│   ├── train.py
│   ├── distributed/
│   └── scheduler/
│
├── evaluation/
│   ├── walk_forward/
│   ├── metrics/
│   ├── stress_test/
│   └── reports/
│
├── backtest/
│   ├── engine/
│   ├── cost/
│   └── execution/
│
├── portfolio/
│   ├── ranking/
│   ├── optimizer/
│   └── constraints/
│
├── serving/
│   ├── inference/
│   ├── signal/
│   └── model_registry/
│
├── execution/
│   ├── paper/
│   ├── qmt/
│   └── broker_adapter.py
│
├── risk/
│   ├── pre_trade/
│   ├── post_trade/
│   └── limits/
│
├── monitoring/
│   ├── logging/
│   ├── alert/
│   └── reconciliation/
│
├── tests/
│   ├── test_data_leakage.py
│   ├── test_backtest.py
│   ├── test_execution.py
│   └── test_risk.py
│
└── artifacts/
    ├── experiments/
    ├── models/
    └── reports/
```

---

# 27. 开发阶段

## Phase 0 — 基础设施

完成：

```text
Repository
Python Environment
GPU Environment
PyTorch
Qlib
Data Storage
Logging
Config System
```

验收：

```text
8 张 GPU 可被独立调度
单卡训练正常
数据能够完整读取
```

---

# 28. Phase 1 — 数据

完成：

```text
日线行情
复权
历史股票池
ST
停牌
涨跌停
交易日历
```

验收：

```text
随机抽取股票人工核对
时间字段正确
不存在明显未来数据
```

---

# 29. Phase 2 — Baseline

实现：

```text
Alpha158
+
LightGBM
```

以及：

```text
MLP
```

输出第一份：

```text
Walk-Forward Report
```

---

# 30. Phase 3 — 深度模型

实现：

```text
MASTER
or
Custom Transformer
```

必须和 Baseline 对比。

如果无增量：

```text
停止扩展
分析失败原因
```

而不是强行加复杂度。

---

# 31. Phase 4 — 自动 Alpha

接入：

```text
RD-Agent style workflow
```

目标：

```text
自动提出
自动测试
自动淘汰
自动记录
```

不直接进实盘。

---

# 32. Phase 5 — Foundation Model

实验：

```text
Kronos / TimesFM / Chronos / Moirai
```

比较：

```text
Zero-shot
Feature Extraction
Fine-tune
```

---

# 33. Phase 6 — Ensemble

只有前面存在真实增量后：

```text
LightGBM
+
Transformer
+
Foundation Feature
+
Agent Factor
```

---

# 34. Phase 7 — Paper Trading

每天完整运行：

```text
数据更新
↓
特征更新
↓
模型预测
↓
目标持仓
↓
模拟交易
↓
订单记录
↓
持仓核对
```

至少经历：

```text
20 个交易日
+
4 次以上调仓
```

这里主要验证工程，不作为长期收益统计依据。

---

# 35. Phase 8 — Live Trading

接入：

```text
QMT / miniQMT
```

初始采用：

```text
小资金
低频
低换手
人工可随时接管
```

---

# 36. 研究原则

整个项目始终遵循：

## Simple First

复杂模型必须击败简单模型。

## Out-of-Sample First

样本外结果优先于训练集结果。

## Cost First

忽略交易成本的收益不算真实收益。

## Reproducibility

每个结果必须能重新跑出来。

## Version Everything

保存：

```text
Data Version
Feature Version
Model Version
Code Commit
Config
Random Seed
```

## No Cherry Picking

失败实验必须保留。

## Human Approval Before Live

任何模型升级进入实盘前都必须人工确认。

---

# 37. 第一版最小可行产品

MVP 只做：

```text
A 股
沪深 300
日频
Alpha158
LightGBM
MLP
1 个 Transformer
5 日预测
Walk-Forward
成本回测
Paper Trading
```

暂时不做：

```text
新闻
LLM
GNN
Level-2
分钟高频
自动实盘 Agent
```

这些作为后续实验逐一加入。

---

# 38. 第一阶段真正要回答的问题

项目启动后第一个真正重要的问题不是：

```text
能赚多少钱？
```

而是：

```text
在严格无泄漏、
真实交易约束、
包含成本、
多个 Walk-Forward 区间的情况下，

复杂模型是否能够稳定优于：
Alpha158 + LightGBM？
```

如果答案是：

```text
No
```

那么这也是一个非常有价值的实验结论。

如果答案是：

```text
Yes
```

才继续增加模型复杂度。

---

# 39. 最终目标

长期目标不是构建一个：

```text
AI 猜股票系统
```

而是构建一个：

```text
Quant Research Factory
```

能够持续执行：

```text
发现假设
↓
生成因子
↓
训练模型
↓
严格验证
↓
淘汰无效策略
↓
保留稳健信号
↓
组合模型
↓
轻量部署
↓
实盘验证
↓
反馈到研究系统
```

最终形成：

> **8 GPU 重研究 + CPU / 单 GPU 轻推理 + 独立交易执行 + 独立风控**

的完整个人 A 股量化研究平台。

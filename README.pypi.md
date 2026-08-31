# FinePrint

**Read the fine print of your metrics — a decompiler for your dashboards.**

FinePrint recovers, from your dbt project's compiled artifacts, the caliber a
metric **actually executes** — its true definition, in the small print of the SQL.

It can tell you:

- how a metric's formula is computed;
- where the numerator and the denominator each come from;
- which filters, time windows and dedup rules shape the result;
- which upstream columns the metric depends on;
- whether a SQL change silently changed the metric's meaning.

FinePrint never connects to your database and never reads your business data.

## Example

The dashboard says the refund rate is 30%. Finance computes 80%.

Walking the SQL chain reveals what the dashboard actually counts:

- only refunds issued within 14 days of payment;
- only the latest record of each refund;
- test orders excluded;
- paid orders only;
- attributed to the payment date, not the refund date.

FinePrint unfolds those conditions, buried across multiple layers of SQL, directly:

```text
$ fineprint trace --project . dm_refund_rate_1d.refund_rate

◎ dm.dm_refund_rate_1d.refund_rate
│  formula: SUM(refund_amount) / SUM(amount)
│
├─ numerator
│  ├─ refunded_at <= paid_at + INTERVAL '14' DAY
│  └─ rn = 1
│
├─ denominator
│  └─ from stg_orders
│
└─ shared by both sides
   ├─ status = 'paid'
   └─ is_test = 0
```

This caliber tree is derived from the SQL by a deterministic program — no LLM involved.

## Core capabilities

### Metric caliber tracing

Column-level lineage built on `sqlglot`, unfolding across models:

- the formula;
- numerator / denominator;
- filter conditions;
- time windows;
- column dependencies.

```bash
fineprint graph --project .
fineprint trace --project . model.column
```

### Metric caliber cards

FinePrint runs two independent channels:

- a **deterministic engine** deriving technical facts from the SQL AST;
- an **LLM reader** producing a business-readable narrative.

The two are cross-validated before a traceable caliber card is published.

```bash
fineprint synth --project .
fineprint report --project .
```

`synth` sends the relevant compiled SQL and column documentation to the LLM
endpoint you configure; database credentials and warehouse data are never sent.
Every other command runs entirely locally.

### Caliber drift detection

Detects:

- formula changes;
- filter changes;
- time-window changes;
- source-column and dependency changes;
- the downstream metrics affected.

```bash
fineprint drift --project .
```

`--strict` makes it a CI gate.

## Accuracy

The deterministic engine has been probed exhaustively on:

- 5 public dbt projects;
- 3 SQL dialects;
- 1,364 models;
- 34,499 columns.

Provable cross-layer formula coverage: **99.73%**. (Coverage measures formula
provability — it is not the same thing as business-caliber accuracy.)

A hand-built suite of 14 classic caliber traps serves as regression:
currently **14 / 14**.

## Install

```bash
pip install fineprint
```

## Quick start

```bash
cd your-dbt-project

dbt compile
dbt docs generate

fineprint init --project .
fineprint graph --project .
fineprint trace --project . model.column
```

To generate caliber cards:

```bash
export FINEPRINT_LLM_BASE_URL=https://api.openai.com/v1
export FINEPRINT_LLM_API_KEY=sk-...
export FINEPRINT_LLM_MODEL=gpt-4.1-mini

fineprint synth --project .
fineprint report --project .
```

After changing SQL:

```bash
dbt compile
fineprint graph --project .
fineprint drift --project .
```

`graph`, `trace` and `drift` never call an LLM.

## Python API

Since 0.9, a minimal public surface for notebooks, BI plugins and orchestration:

```python
import fineprint

fineprint.build_graph("path/to/dbt_project")
print(fineprint.trace("path/to/dbt_project", "dm_refund_rate_1d.refund_rate"))
batch = fineprint.cards("path/to/dbt_project")   # the card JSON is the contract (schema_version frozen)
```

## Current boundaries

What FinePrint recovers is:

> **the caliber your code actually executes.**

It cannot judge on its own whether the SQL matches the business's original intent.

The current stable release targets dbt projects.

License: **Apache-2.0**

---

# FinePrint（中文说明）

**读懂指标的小字条款——给看板的反编译器。**

FinePrint 从 dbt 的编译产物中，还原一个指标**真正执行的口径**。

它可以告诉你：

- 指标公式是怎么计算的；
- 分子、分母分别来自哪里；
- 哪些过滤条件、时间窗口和去重逻辑影响了结果；
- 指标依赖了哪些上游字段；
- 一次 SQL 修改是否改变了指标口径。

FinePrint 不连接数据库，也不读取业务数据。

## 示例

看板上的退款率是 30%，财务计算却是 80%。

沿着 SQL 链路排查后发现，看板实际上只统计：

- 支付后 14 天内发生的退款；
- 每个退款单的最新记录；
- 非测试订单；
- 支付成功订单；
- 并按支付日期而不是退款日期归属。

FinePrint 可以直接把这些隐藏在多层 SQL 中的条件展开：
```text
$ fineprint trace --project . dm_refund_rate_1d.refund_rate

◎ dm.dm_refund_rate_1d.refund_rate
│  公式: SUM(refund_amount) / SUM(amount)
│
├─ 分子
│  ├─ refunded_at <= paid_at + INTERVAL '14' DAY
│  └─ rn = 1
│
├─ 分母
│  └─ 来自 stg_orders
│
└─ 共同口径
   ├─ status = 'paid'
   └─ is_test = 0
```

这棵口径树由确定性程序直接从 SQL 推导，不依赖 LLM。

## 核心能力

### 指标口径追踪

基于 `sqlglot` 构建字段级血缘，展开跨模型 SQL 中的：

- 公式；
- 分子 / 分母；
- 过滤条件；
- 时间窗口；
- 字段依赖。

```bash
fineprint graph --project .
fineprint trace --project . model.column
```

### 指标口径卡

FinePrint 使用两条独立通道：

- **确定性引擎**：从 SQL AST 推导技术事实；
- **LLM 解读**：生成业务可读的口径说明。

两者互验后生成可追溯的指标口径卡。

```bash
fineprint synth --project .
fineprint report --project .
```

`synth` 会把相关 compiled SQL 与字段注释发送到你配置的 LLM 端点；数据库凭据与数仓数据从不发送，其余命令全程本地。

### 指标口径漂移检测

检测：

- 公式变化；
- 过滤条件变化；
- 时间窗口变化；
- 源字段和依赖变化；
- 受影响的下游指标。

```bash
fineprint drift --project .
```

`--strict` 可用于 CI。

## 准确性

确定性引擎已在：

- 5 个公开 dbt 项目；
- 3 种 SQL 方言；
- 1,364 个模型；
- 34,499 个字段

上进行全量测试。

跨层公式可证明覆盖率：**99.73%**。（覆盖率说的是公式可证明性，不等同于业务口径准确率。）

另外构建了 14 类典型指标口径陷阱的测试集，当前结果：**14 / 14**。

## 安装

```bash
pip install fineprint
```

## 快速开始

```bash
cd your-dbt-project

dbt compile
dbt docs generate

fineprint init --project .
fineprint graph --project .
fineprint trace --project . model.column
```

生成指标口径卡：

```bash
export FINEPRINT_LLM_BASE_URL=https://api.deepseek.com/v1
export FINEPRINT_LLM_API_KEY=sk-...
export FINEPRINT_LLM_MODEL=deepseek-chat

fineprint synth --project .
fineprint report --project .
```

SQL 修改后：

```bash
dbt compile
fineprint graph --project .
fineprint drift --project .
```

`graph`、`trace`、`drift` 完全不调用 LLM。

## Python API

0.9 起提供最小公开面，适合 notebook、BI 插件与编排任务：

```python
import fineprint

fineprint.build_graph("path/to/dbt_project")
print(fineprint.trace("path/to/dbt_project", "dm_refund_rate_1d.refund_rate"))
batch = fineprint.cards("path/to/dbt_project")   # 口径卡 JSON 即契约(schema_version 冻结)
```

## 当前边界

FinePrint 还原的是：

> **代码实际上执行了什么口径。**

它不能自行判断 SQL 是否符合业务最初的设计意图。

当前稳定版本主要面向 dbt 项目。

License: **Apache-2.0**
# FinePrint

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

想亲手跑一遍？[内置示例工程](https://github.com/longcanxiao/fineprint/tree/main/examples/quickstart) 10 分钟走完全流程——预编译产物已内置，不装 dbt、不建数据库。不检出仓库也行：`pip install fineprint && fineprint init --demo`。

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

需要 Python 3.10+。

```bash
pip install fineprint
```

## 快速开始

手边没有 dbt 项目？先跑内置示例——不装 dbt、不连数据库、不配 LLM key
（示例自带一份现成口径批次）：

```bash
fineprint init --demo && cd fineprint-quickstart

fineprint graph
fineprint trace dm_refund_rate_1d.refund_rate
fineprint report
```

在你自己的 dbt 项目上：

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

问题与需求欢迎提 [GitHub Issues](https://github.com/longcanxiao/fineprint/issues)。

License: **Apache-2.0**
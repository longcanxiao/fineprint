# FinePrint

读懂指标的小字条款——给看板的反编译器。
Read the fine print of your metrics. A decompiler for your dashboards.

## 一个「退款率」的真实故事

财务在财报会上问：退款率怎么飚到 80% 了？业务负责人愣住：我们看板上一直是 30% 啊。

数据工程师被拉进来，翻了业务看板的整条加工链路，发现退款率的完整口径是：

- 只统计支付后 14 天内发生的退款；
- 同一退款单有多条变更记录，只保留最新一条；
- 测试订单被排除；
- 分母只包含支付成功订单；
- 结果按支付日期归属，而不是按退款日期归属。

14 天限制的魔鬼细节导致了 30% 与 80% 的巨大差别。FinePrint 要做的事：

> **把 SQL 里那些“小字”读出来、摆到台面上、并盯住每一次变动。**

一条命令，上面那个退款率变成这样：

```text
$ fineprint trace --project . dm_refund_rate_1d.refund_rate

◎ dm.dm_refund_rate_1d.refund_rate
│  输出维度: stat_date = CAST(paid_at AS DATE)
│  公式: SUM(COALESCE(refund_amount, 0)) / SUM(raw_orders.amount)
│
├─ 分子  SUM(COALESCE(refund_amount, 0))
│  ├─ 其中 refund_amount = SUM(raw_refunds.refund_amount) 按 order_id 聚合(经 join)
│  ├─ 口径: r.refunded_at <= o.paid_at + INTERVAL '14' DAY   (dm_refund_rate_1d.refund_14d)
│  ├─ 口径: rn = 1   (stg_refunds)
│  └─ 链路: stg_refunds → 本层
│
├─ 分母  SUM(raw_orders.amount)
│  └─ 链路: stg_orders → 本层
│
└─ 两侧共同口径
   ├─ status = 'paid'   (dm_refund_rate_1d.paid_orders)
   └─ is_test = 0   (stg_orders)
```

这棵树完全由确定性程序从 SQL 推导而来——不经过任何 LLM，每一行都可追溯、可验证。

## 它是怎么做到的？

FinePrint 只读取 dbt 的编译产物（manifest.json / catalog.json / compiled SQL），从不连接数据库。两条独立的通道互相校验：

**通道一（确定性引擎）**：基于 sqlglot 构建字段级血缘，穿透 CTE、子查询、UNION、PIVOT，抽取出每个指标的源字段、过滤条件和表达式链。一个确定性公式组合器直接从 AST 合成跨层展开的指标公式——每条事实都附带「文件 + 编译行号」的证据。

**通道二（LLM 解读）**：逐模型阅读 SQL，生成业务视角的口径叙述。所有原文引用逐条机器校验，如果引用不在原文中，直接判定为幻觉并拒绝输出。

**互验与发布状态机**：两通道逐条比对后给出三个定级：

- ✅ `VERIFIED`：机器事实与 LLM 解读完全一致
- 📝 `TECHNICAL_ONLY`：机器事实可发布，叙述待人工审核
- ⚠️ `REVIEW_REQUIRED`：存在歧义，暂不予发布

公式的发布权威永远是确定性引擎，LLM 只负责解释；只有当确定性引擎覆盖不到时（比如标量子查询、零列声明的裸列归属），才由 LLM 兜底，并在卡片上明确标注。

## 你能得到什么

### 1. 可阅读的指标口径树

把跨模型 SQL 展开成分子、分母、中间定义和共同过滤条件。适合排查口径、代码评审，
也适合在 Notebook 中快速理解陌生指标。

### 2. 可分享的指标口径卡

口径卡同时包含：

- 面向业务人员的口径说明；
- 由确定性组合器生成的权威技术公式；
- 关键过滤、时间窗口和输出粒度；
- 业务条款对应的证据编号；
- 模型文件、编译行号和证据原文；
- 双通道互验结果与发布状态。

报告导出为自包含 HTML 文件，可以直接浏览或分享，不依赖单独的前端服务。

### 3. 指标口径漂移检测

FinePrint 对血缘图和语义点建立快照。重新编译和建图后，`drift` 可以识别：

- 公式变化；
- 过滤条件新增、删除或修改；
- 时间窗口变化；
- 源字段和依赖链路变化；
- 受影响的下游指标。

`--strict` 可用于 CI：发现高风险漂移时返回非零退出码，阻止未经确认的口径变更发布。

## 覆盖率和准确性

确定性引擎（不靠 LLM 的那一半）在 5 个公开项目、3 种 SQL 方言、34,499 列上做了全量测试：每一列都尝试完整合成跨层公式，能证明则计为 proven。

| 项目 | 性质 | 方言 | 模型数 | 列数 | proven |
|---|---|---|---:|---:|---:|
| Cal-ITP warehouse | 真实生产（加州交通数据平台） | BigQuery | 604 | 16,856 | 99.94% |
| Mattermost analytics | 真实生产 | Snowflake | 144 | 3,907 | 99.92% |
| Mattermost snowflake-dbt | 真实生产（遗留数仓） | Snowflake | 214 | 5,180 | 98.44% |
| Fivetran ad_reporting | 开源 dbt 包套件（12 包） | Postgres | 350 | 6,771 | 100% |
| Snowplow web | 开源 dbt 包 | Snowflake | 52 | 1,785 | 100% |
| **合计** | | | **1,364** | **34,499** | **99.73%** |

真实生产项目意味着真实的复杂度——Cal-ITP 里满是 UNNEST、STRUCT、date-spine、PIVOT，组合器照样能给出确定性展开。例如一个 PIVOT 列：

```text
trips_owl := MIN(CASE WHEN time_of_day = 'owl' THEN n_trips END)
             per [key, service_date, route_id, direction_id]
```

残余的 0.27% 全部具名归因：标量子查询（内部口径不做组合声明）、命名子表达式规模超限、遗留数仓中零列声明导致的裸列归属问题（这恰好是 LLM 兜底通道的正当应用场景）。

FinePrint 清楚自己不会什么，并把它写进产物里。

我们用自建的 **14 道口径陷阱金集**做验收：同名不同义（当日退款率 vs 14 天退款率）、JOIN 条件里藏的状态限定、binlog 多版本窗口去重、SCD2 汇率区间 JOIN、按人均摊 vs 按单均摊……口径卡对 14/14 全部揭示（验收线 ≥ 12）。

## 快速开始

```bash
pip install fineprint
```

### 路径一：你有完整的 dbt 项目（推荐）

```bash
cd your-dbt-project
dbt compile && dbt docs generate      # 生成 FinePrint 需要的编译产物
fineprint init  --project .           # 生成 fineprint.yml,声明你的指标
fineprint graph --project .           # 建字段级血缘图
fineprint trace --project . model.column   # 看任意一列的口径树(零 LLM)
```

口径卡与漂移需要一个 OpenAI 兼容端点（环境变量或项目根 `.env`）：

```bash
export FINEPRINT_LLM_BASE_URL=https://api.deepseek.com/v1
export FINEPRINT_LLM_API_KEY=sk-...
export FINEPRINT_LLM_MODEL=deepseek-chat
fineprint synth  --project .          # 双通道合成口径卡,整批原子发布
fineprint report --project .          # 导出自包含 HTML 报告
fineprint drift  --project .          # 改完 SQL 重新 compile+graph 后跑:口径漂移对比
```

### 路径二：你只有编译产物，连不上数据库

FinePrint 本来就不连数据库；即使 `catalog.json` 也没有（跑不了 `dbt docs generate`），在 `fineprint.yml` 里声明源表列集即可建图——血缘、口径树、口径卡全流程照常，只是列集推断少一路校验。

`graph` / `trace` / `drift` 完全零 LLM；`synth` 的 LLM 用量按模型数计，内容寻址缓存，重跑只付增量。

### 路径三：体验 demo

仓库自带一个完整示例工程（订单/退款两份原始数据、4 个 dbt 模型、2 个看板指标，预编译产物已内置）：不装 dbt、不建数据库，10 分钟走完 graph → trace → synth → report → drift 全流程——包括把 14 天窗口改成 30 天、亲眼看漂移检测点名口径变更的实验。

<!-- TODO(仓库公开后): 附 examples/quickstart 链接 -->

### 在 notebook / BI 插件 / 编排任务里，用 Python API（0.9 起的最小公开面）

```python
import fineprint

fineprint.build_graph("path/to/dbt_project")        # 建血缘图(零 LLM)
print(fineprint.trace("path/to/dbt_project",
                      "dm_refund_rate_1d.refund_rate"))   # 就是上面那棵口径树
batch = fineprint.cards("path/to/dbt_project")      # 已发布的口径卡批次
batch["refund_rate_14d"]["technical_facts"]         # 卡片 JSON 即契约(schema_version 冻结)
```

## 数据与隐私边界

FinePrint 核心流程不连接数据库，也不会读取数仓中的业务数据。

- `graph`、`trace`、`drift`：完全本地运行，不调用 LLM；
- `synth`：会把相关 compiled SQL、schema.yml 字段说明、`fineprint.yml` 业务词典、
  指标上下文和确定性证据发送到你配置的 LLM 端点；
- 数据库账号、数据库密码和数仓中的实际数据不会发送给 LLM；
- LLM 响应会按内容寻址缓存在 `.fineprint/cache/`，该目录可能包含 SQL 片段，应按
  源代码同等级别管理；
- SQL 敏感时，应使用自托管、VPC 内或符合组织安全要求的模型端点。

## 适用范围与当前边界

FinePrint 当前最适合：

- 已经使用 dbt、但指标定义散落在多层 SQL 中的团队；
- 需要快速理解陌生指标的数据开发和分析师；
- 希望在代码评审或 CI 中发现口径变化的团队；
- 希望把指标口径接入 Notebook、数据目录或治理平台的开发者。

当前版本的边界：

- FinePrint 还原“代码实际执行的口径”，但无法判断原始 SQL 是否符合业务最初意图；
- SQL 和文档中从未出现的业务含义，需要通过 schema.yml、`fineprint.yml` 业务词典
  或人工审核补充；
- 非 dbt SQL 管道和 BI 层血缘尚未纳入当前稳定发行版。

## Roadmap

- 重复指标识别与仲裁；
- dbt exposures 消费方标注；
- 非 dbt SQL 管道适配；
- BI 层血缘。

License: **Apache-2.0**

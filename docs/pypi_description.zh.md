# FinePrint

**Read the fine print of your metrics. A decompiler for your dashboards.**
**读懂指标的小字条款——给看板的反编译器。**

---

## 从一个「14 天退款率」说起

你的看板上有一个「退款率」。业务方问:这个数到底怎么算的?

你打开 `dm_refund_rate_1d.sql`,分子是 `SUM(COALESCE(refund_amount, 0))`,看起来很简单。但真正的口径散落在别处:

- 退款只算**支付后 14 天内**发生的——藏在一个 CTE 里的 `INTERVAL '14' DAY`(第 15 行);
- 同一笔退款有多条上报,取了 binlog 最新一条——上游 `stg_refunds.sql` 的 `rn = 1`(第 13 行);
- 测试账号被剔掉了——`stg_orders.sql` 的 `is_test = 0`(第 9 行);
- 分母只算已支付订单——`status = 'paid'`(第 6 行)。

四个条件,散在 3 个模型、两层加工里,没有任何一处文档完整写过它们。分析师以为「退款率 = 退款 / 成交」;数据工程师要翻完整条链路才能答全;而当有人把 14 天改成 30 天时,看板不会有任何提示——数字只是悄悄变了。

**指标真正的定义写在 SQL 的小字里。FinePrint 把小字读出来、放到台面上、并盯住它的每一次变动。**

一条命令,上面那个退款率变成这样:

```
$ fineprint trace --project . dm_refund_rate_1d.refund_rate

◎ dm.dm_refund_rate_1d.refund_rate
│  输出维度: stat_date = CAST(paid_at AS DATE)
│  公式: A / B
│
├─ A 分子  SUM(COALESCE(refund_amount, 0))
│  ├─ 其中 refund_amount = SUM(raw_refunds.refund_amount) 按 order_id 聚合(经 join)
│  ├─ 口径: r.refunded_at <= o.paid_at + INTERVAL '14' DAY   (dm_refund_rate_1d.refund_14d)
│  ├─ 口径: rn = 1   (stg_refunds)
│  └─ 链路: stg_refunds → 本层
│
├─ B 分母  SUM(raw_orders.amount)
│  └─ 链路: stg_orders → 本层
│
└─ 两侧共同口径
   ├─ status = 'paid'   (dm_refund_rate_1d.paid_orders)
   └─ is_test = 0   (stg_orders)
```

分子的 14 天窗、分母的口径、两侧共享的过滤,各归各位;`--full` 会在每一行附上源文件与编译行号。这棵树完全由确定性程序从 SQL 推出——不经过任何 LLM,每一行都可回放验证。

## 它是怎么工作的

FinePrint 只读 dbt 的编译产物(`manifest.json` / `catalog.json` / compiled SQL),**从不连接数据库**。两条独立通道互相对账:

- **通道一(确定性)**:基于 sqlglot 的字段级血缘,穿透 CTE / 子查询 / UNION / PIVOT,抽取每个指标的源字段、过滤条件、表达式链;一个**确定性公式组合器**直接从 AST 合成跨层展开的指标公式——每条事实都带「文件 + 编译行号」证据。
- **通道二(LLM)**:逐模型阅读 SQL,生成业务视角的口径叙述;所有原文引用逐条机器校验,引用不在原文中即判幻觉。
- **互验与发布状态机**:两通道逐条比对后定级——`VERIFIED` / `TECHNICAL_ONLY`(机器事实可发布,叙述待审)/ `REVIEW_REQUIRED`(不予发布)。**公式的发布权威永远是组合器**,LLM 只负责解释;组合器覆盖不到时才由 LLM 兜底,并在卡片上明确标注。

三个产物:终端里的**口径树**(上图)、可分享的**口径卡 HTML 报告**(业务+技术双口径,每条条款可点击跳转到证据原文与编译行),以及**口径漂移日志**(图快照逐语义点比对,SQL 改动直接落到受影响的指标与条件粒度)。示例仓里有人把 14 天窗口悄悄改成 30 天:那天的退款率从 30% 变成 80%,看板毫无提示——`fineprint drift` 会点名这条口径变更和它波及的每个指标。

<!-- TODO(仓库公开后): 此处插入口径卡 HTML 报告截图 -->

## 覆盖率与准确性

确定性组合器(不靠 LLM 的那一半)在 5 个公开项目、3 种方言、34,499 个列上做了全量探针:每一列都尝试完整合成跨层公式,能证明则计 proven。

| 语料 | 性质 | 方言 | 模型 | 列 | proven |
|---|---|---|---|---:|---:|
| Cal-ITP warehouse | 真实生产(加州交通数据平台) | BigQuery | 604 | 16,856 | 99.94% |
| Mattermost analytics | 真实生产 | Snowflake | 144 | 3,907 | 99.92% |
| Mattermost snowflake-dbt | 真实生产(legacy 仓) | Snowflake | 214 | 5,180 | 98.44% |
| Fivetran ad_reporting | 开源 dbt 包套件(12 包) | Postgres | 350 | 6,771 | 100% |
| Snowplow web | 开源 dbt 包 | Snowflake | 52 | 1,785 | 100% |
| **合计** | | | **1,364** | **34,499** | **99.73%** |

真实生产仓意味着真实的复杂度——Cal-ITP 里满是 UNNEST / STRUCT / date-spine / PIVOT,组合器照样给出确定性展开,比如一个 PIVOT 列:

```
trips_owl := MIN(CASE WHEN time_of_day = 'owl' THEN n_trips END)
             per [key, service_date, route_id, direction_id]
```

残余 0.27% 全部具名归因——标量子查询(内部口径不做组合声明)、命名子表达式规模超限、legacy 仓零列声明源表的裸列归属(需要业务世界知识,正是 LLM 兜底通道的正当场景)。工具知道自己不会什么,并写在产物里。

准确性用自建的 14 道口径陷阱金集验收:同名不同义(当日退款率 vs 14 天退款率)、join `on` 里藏的状态限定、binlog 多版本窗口去重、SCD2 汇率区间 join、按人均摊 vs 按单均摊……口径卡对 14/14 全部揭示(验收线 ≥12)。

## 快速开始

```bash
pip install fineprint
```

**路径 1:你有能跑 dbt 的项目(推荐)**

```bash
cd your-dbt-project
dbt compile && dbt docs generate      # 生成 FinePrint 需要的编译产物
fineprint init  --project .           # 生成 fineprint.yml,声明你的指标
fineprint graph --project .           # 建字段级血缘图
fineprint trace --project . model.column   # 看任意一列的口径树(零 LLM)
```

口径卡与漂移需要一个 OpenAI 兼容端点(环境变量或项目根 `.env`):

```bash
export FINEPRINT_LLM_BASE_URL=https://api.deepseek.com/v1
export FINEPRINT_LLM_API_KEY=sk-...
export FINEPRINT_LLM_MODEL=deepseek-chat
fineprint synth  --project .          # 双通道合成口径卡,整批原子发布
fineprint report --project .          # 导出自包含 HTML 报告
fineprint drift  --project .          # 改完 SQL 重新 compile+graph 后跑:口径漂移对比
```

**路径 2:只有编译产物,连不上库**

FinePrint 本来就不连数据库;即使 `catalog.json` 也没有(跑不了 `dbt docs generate`),在 `fineprint.yml` 里声明源表列集即可建图——血缘、口径树、口径卡全流程照常,只是列集推断少一路校验。

`graph` / `trace` / `drift` 完全零 LLM;`synth` 的 LLM 用量按模型数计,内容寻址缓存,重跑只付增量。

**在 notebook / BI 插件 / 编排任务里,用 Python API**(0.9 起的最小公开面):

```python
import fineprint

fineprint.build_graph("path/to/dbt_project")        # 建血缘图(零 LLM)
print(fineprint.trace("path/to/dbt_project",
                      "dm_refund_rate_1d.refund_rate"))   # 就是上面那棵口径树
batch = fineprint.cards("path/to/dbt_project")      # 已发布的口径卡批次
batch["refund_rate_14d"]["technical_facts"]         # 卡片 JSON 即契约(schema_version 冻结)
```

<!-- TODO(仓库公开后): 附 examples/quickstart 链接——自带数据与预编译产物的完整示例工程 -->

## Roadmap

- **重复指标治理**(指纹扫描 + LLM 仲裁,`fineprint govern`)与 **dbt exposures 集成**(看板消费方标注)——2.0 主题,仓库内已有原型;
- **非 dbt SQL 管道**适配(任务式调度平台的 SQL + 产出表元数据);
- BI 层血缘。

有具体诉求欢迎联系:<联系方式待定稿时填>。

License: **Apache-2.0**

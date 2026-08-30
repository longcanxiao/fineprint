# fineprint 快速体验(10 分钟,零数据库)

这个目录是一个微型电商数仓:订单、退款两份原始数据,四个 dbt 模型,
两个看板指标——**每日GMV** 和 **14天退款率**。你要体验的问题只有一个:

> *"这个退款率到底是什么口径?"*

dbt 编译产物(`target/`)已内置,所以**不需要安装 dbt、不需要任何数据库**。
前两步连 LLM 都不需要。

## 0. 安装

任意 Python 3.10+ 环境:

```bash
cd examples/quickstart          # 进入本目录(整个目录拷到哪里都能跑)
python3 -m venv .venv && source .venv/bin/activate
pip install fineprint
```

## 1. 建字段级血缘图(秒级,零 LLM)

```bash
fineprint graph
```

```
graph: 4 models, 15 columns, 7 conditions, 5 semantic points → .fineprint/graph.json  (dialect=duckdb)
```

## 2. 追口径:退款率的小字条款

```bash
fineprint trace dm_refund_rate_1d.refund_rate
```

```
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

公式在最外层运算处劈成分子/分母两支,小字条款各归其位:14 天窗口和
去重是**分子专属**;"只算支付成功""剔测试账号"约束的是两侧共同的行集,
归入**公共口径**(分子经 join 关联订单,同样被它们过滤——这正是只看
值路径会归错的地方)。想核对每条口径的 SQL 原文与源文件行号,加 `--full`
即可在树下附完整出处明细(表达式链 E/源字段 S/逐条过滤条件 F)。

这些条款不是摆设——本例数据里有一笔 200 元的退款发生在支付后第 19 天:
14 天口径下 8 月 1 日退款率是 **30%**,若是 30 天口径就是 **80%**。

## 3. 配置 LLM(下一步需要)

```bash
cp .env.example .env    # 打开 .env 填入你的 key(OpenAI 风格 API 均可,如 DeepSeek)
```

## 4. 双通道合成口径卡

```bash
fineprint synth
```

```
✓ daily_gmv         conf=high  F覆盖 100%  S漏/多 0/0  可疑 0  未证条款 0  词表失配 0  赛马 agree→VERIFIED
✓ refund_rate_14d   conf=high  F覆盖 100%  S漏/多 0/0  可疑 0  未证条款 0  词表失配 0  赛马 agree→VERIFIED
双写赛马: agree=2
发布状态: VERIFIED=2
```

两条通道在此汇合:**确定性公式组合器**(发布权威)从编译 SQL 逐层展开出
机器可证的公式;**LLM** 通读同一条链路给出业务解读。二者互证一致
(`agree`)、LLM 的每句话都能溯源到血缘词表,卡片才盖 `VERIFIED`。

## 5. 导出口径卡报告

```bash
fineprint report
open .fineprint/caliber_report.html     # Windows: start,Linux: xdg-open
```

## 6. 漂移实验:有人悄悄把 14 天改成 30 天

先建基线,再动手脚:

```bash
fineprint drift        # 首次运行:基线快照已建立
```

打开 `target/compiled/fineprint_quickstart/models/dm/dm_refund_rate_1d.sql`,
把 `INTERVAL 14 DAY` 改成 `INTERVAL 30 DAY`,然后:

```bash
fineprint graph && fineprint drift
```

```
口径漂移检测: 2 个事件

  ⚠ [high  ] refund_rate_14d   condition_removed  r.refunded_at <= o.paid_at + INTERVAL '14' DAY
  ⚠ [high  ] refund_rate_14d   condition_added    r.refunded_at <= o.paid_at + INTERVAL '30' DAY
```

改动落到了受影响的指标与具体条件上——不是"某个文件变了",而是
"14天退款率的时间窗被从 14 天改成了 30 天"。体验完把 SQL 改回去即可。

> 真实项目里你改的是 `models/*.sql`,`dbt compile` 之后 fineprint 读到的
> 编译产物随之变化;这里直接改编译产物,是为了让你不装 dbt 也能体验。

## 7.(可选)从零重建数仓

想看完整链路(改模型 SQL → dbt 编译 → 口径变化),装上 dbt 即可:

```bash
pip install dbt-duckdb
dbt seed --profiles-dir . && dbt run --profiles-dir . && dbt docs generate --profiles-dir .
```

---

**备注**
- 所有产物在 `.fineprint/`:血缘图、口径卡 JSON(`store/runs/<批次>/`)、
  HTML 报告、漂移快照与日志。
- 命令统一为 `fineprint`(0.8.4 起;旧 `metriclens` 命令与 import 名一并退役)。
- PyPI 发行版为核心版:重复指标治理(`fineprint govern`)与 dbt exposures
  集成定于 2.0,不随包发布,CLI 中也不出现。

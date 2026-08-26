# 「近14天退款率」生产链路核对单

> 目标列:`app.app_business_overview_1d.refund_rate_14d` · 链路 5 层 · 8 个模型 · 5 个 ODS 源字段
> 页面口径卡:**high 置信**,生成于 2026-08-25T16:50:37,双通道互验 F 覆盖 100%

## 0. 链路总览(数据流向)

```
分母支路(交易域,统计日=支付日)                 分子支路(售后域,统计日=退款完成日)
─────────────────────────────                 ─────────────────────────────
ods_order_info ─┐                              ods_refund_apply ─┐
ods_user_info ──┤                              ods_refund_payment┤
ods_exchange_rate┤                                               │
                ▼                                                ▼
dwd_trade_order_detail ──────────────────────► dwd_after_refund_detail
  (T9 多版本去重·剔测试/风控·汇率折算)            (T9 申请去重·T13 打款兜底·
                ▼                                T2 join on 状态限定补 pay_time)
ods_payment_flow┤                                                ▼
                ▼                              dwm_refund_order_agg_1d
dwd_trade_pay_suc_detail                         (T2 datediff≤14·T14 按退款完成日)
  (pay_status=SUCCESS·分→元·状态限定)                            ▼
                ▼                              dm_after_sale_stats_1d
dwm_trade_order_flag_1d(透传 pay_amt)            (refund_amt_14d 日汇总)
                ▼                                                │
dm_trade_stats_1d(pay_amt 日汇总)                                │
                └────────────► app_business_overview_1d ◄────────┘
                     refund_rate_14d = refund_amt_14d / pay_amt(按 dt left join)
```

## 1. 数值锚点(核对起点,可直接复算)

| 日期 | 分子 refund_amt_14d | 分母 pay_amt | 页面比率 |
|---|---|---|---|
| 2026-06-18 | 55,390.23 | 3,892,080.27 | 1.4232% |
| 2026-08-10 | 69,344.86 | 1,643,154.15 | 4.2202% |

复算命令(任一日期):

```bash
.venv/bin/python -c "import duckdb; print(duckdb.connect('warehouse/metriclens.duckdb', read_only=True).execute(\"select a.refund_amt_14d, t.pay_amt, round(a.refund_amt_14d/t.pay_amt,6) from dm.dm_after_sale_stats_1d a join dm.dm_trade_stats_1d t using(dt) where dt='2026-08-10'\").fetchone())"
```

## 2. 逐层 SQL(按数据流向,行号即页面溯源引用行号)

ODS 层为模拟器产物(非 SQL),字段口径见 `warehouse/dbt_project/models/ods/sources.yml`:`ods_refund_apply.refund_apply_amt_cent` / `ods_refund_payment.refund_suc_amt_cent`(单位分,打款金额存在 NULL)、`ods_refund_payment.refund_suc_time`(到账时间锚点)、`ods_order_info.pay_time`、`ods_payment_flow.pay_amt_cent`(单位分)。

### dwd_trade_order_detail(DWD)

**核对点**:L5 订单多版本去重(T9);L10 用户表去重;L41 汇率 SCD2 区间 join(本指标不消费 order_amt_cny,仅路过);**L42 剔测试账号、L43 剔风控单——分子分母共同的隐性前提**。

源文件:`warehouse/dbt_project/models/dwd/dwd_trade_order_detail.sql`(源/编译行号一致,页面溯源引用即编译行号)

```sql
  1  -- 交易域订单明细:多版本去重(T9)、剔除测试/风控单、SCD2 汇率折算(T12)
  2  with ord_latest as (
  3      select *
  4      from {{ source('ods', 'ods_order_info') }}
  5      qualify row_number() over (partition by order_id order by binlog_ts desc) = 1
  6  ),
  7  usr as (
  8      select *
  9      from {{ source('ods', 'ods_user_info') }}
 10      qualify row_number() over (partition by user_id order by binlog_ts desc) = 1
 11  ),
 12  rate as (
 13      select * from {{ source('ods', 'ods_exchange_rate') }}
 14  )
 15  select
 16      o.order_id,
 17      o.user_id,
 18      o.order_time,
 19      o.pay_time,
 20      cast(o.pay_time as date)                       as pay_date,
 21      o.order_status,
 22      o.channel_id,
 23      case when o.live_room_id > 0 then cast(o.live_room_id as bigint) end as live_room_id,
 24      o.live_end_time,
 25      o.category_id,
 26      o.category_name,
 27      o.province,
 28      o.currency,
 29      o.order_amt,
 30      round(o.order_amt * r.rate_to_cny, 2)          as order_amt_cny,
 31      o.item_cnt,
 32      o.is_presale,
 33      u.register_time,
 34      u.register_channel,
 35      o.binlog_ts
 36  from ord_latest o
 37  join usr u
 38    on o.user_id = u.user_id
 39  left join rate r
 40    on o.currency = r.currency
 41   and cast(coalesce(o.pay_time, o.order_time) as date) between r.effective_start and r.effective_end
 42  where coalesce(u.is_test_account, 0) = 0
 43    and o.is_risk_order = 0
```

### dwd_trade_pay_suc_detail(DWD)

**核对点**:L5 流水限定 `pay_status=SUCCESS`;L6 流水按 order_id 去重;**L12 `pay_amt_cent/100.0` 分→元(分母金额的诞生处)**;L16-17 支付时间非空 + 订单状态限定 (20,30,40,50,90)。

源文件:`warehouse/dbt_project/models/dwd/dwd_trade_pay_suc_detail.sql`(源/编译行号一致,页面溯源引用即编译行号)

```sql
  1  -- 支付成功订单明细:关联支付流水,单位分→元
  2  with pf as (
  3      select *
  4      from {{ source('ods', 'ods_payment_flow') }}
  5      where pay_status = 'SUCCESS'
  6      qualify row_number() over (partition by order_id order by pay_time desc) = 1
  7  )
  8  select
  9      o.*,
 10      p.pay_id,
 11      p.pay_type,
 12      p.pay_amt_cent / 100.0 as pay_amt
 13  from {{ ref('dwd_trade_order_detail') }} o
 14  join pf p
 15    on o.order_id = p.order_id
 16  where o.pay_time is not null
 17    and o.order_status in (20, 30, 40, 50, 90)
```

### dwd_after_refund_detail(DWD)

**核对点**:L5 退款申请多版本去重(T9);**L22 `coalesce(打款金额,申请金额)/100` 兜底+分→元(T13,分子金额的诞生处)**;**L25 `datediff(day, pay_time, refund_suc_time)` 窗口变量定义**;L36-37 join 订单补 pay_time 且 **on 中限定订单状态(T2:写在 join on 而非 where)**。

源文件:`warehouse/dbt_project/models/dwd/dwd_after_refund_detail.sql`(源/编译行号一致,页面溯源引用即编译行号)

```sql
  1  -- 售后域退款明细:申请多版本去重(T9)、打款金额兜底(T13)、跨域 join 补支付时间且 on 含状态限定(T2)
  2  with ra as (
  3      select *
  4      from {{ source('ods', 'ods_refund_apply') }}
  5      qualify row_number() over (partition by refund_id order by binlog_ts desc) = 1
  6  ),
  7  rp as (
  8      select * from {{ source('ods', 'ods_refund_payment') }}
  9  )
 10  select
 11      ra.refund_id,
 12      ra.order_id,
 13      ra.user_id,
 14      ra.refund_type,
 15      ra.refund_status,
 16      ra.refund_reason,
 17      ra.apply_time,
 18      rp.refund_suc_time,
 19      cast(rp.refund_suc_time as date)               as refund_suc_date,
 20      ra.refund_apply_amt_cent / 100.0               as refund_apply_amt,
 21      rp.refund_suc_amt_cent / 100.0                 as refund_suc_amt,
 22      coalesce(rp.refund_suc_amt_cent, ra.refund_apply_amt_cent) / 100.0 as refund_amt,
 23      o.pay_time,
 24      o.pay_date,
 25      datediff('day', o.pay_time, rp.refund_suc_time) as days_pay_to_refund,
 26      o.channel_id,
 27      o.live_room_id,
 28      o.category_id,
 29      o.category_name,
 30      o.province,
 31      o.order_amt_cny
 32  from ra
 33  left join rp
 34    on ra.refund_id = rp.refund_id
 35  join {{ ref('dwd_trade_order_detail') }} o
 36    on ra.order_id = o.order_id
 37   and o.order_status in (20, 30, 40, 50, 90)
```

### dwm_trade_order_flag_1d(DWM)

**核对点**:对本指标仅透传 `pay_amt` 与 `dt=pay_date`(秒退旗标/购买序号/归因服务于其他指标,不影响退款率)。

源文件:`warehouse/dbt_project/models/dwm/dwm_trade_order_flag_1d.sql`(源/编译行号一致,页面溯源引用即编译行号)

```sql
  1  -- 交易域支付订单旗标:秒退(T1)/预售/购买序号(T10)/渠道延迟归因(T11)
  2  with pay as (
  3      select * from {{ ref('dwd_trade_pay_suc_detail') }}
  4  ),
  5  ra_latest as (
  6      select *
  7      from {{ source('ods', 'ods_refund_apply') }}
  8      qualify row_number() over (partition by refund_id order by binlog_ts desc) = 1
  9  ),
 10  flash as (
 11      select distinct ra.order_id
 12      from ra_latest ra
 13      join pay p
 14        on ra.order_id = p.order_id
 15       and datediff('second', p.pay_time, ra.apply_time) <= 60
 16  )
 17  select
 18      p.pay_date                                      as dt,
 19      p.order_id,
 20      p.user_id,
 21      p.order_amt_cny,
 22      p.pay_amt,
 23      p.channel_id,
 24      p.live_room_id,
 25      p.category_id,
 26      p.category_name,
 27      p.province,
 28      p.is_presale,
 29      case when f.order_id is not null then 1 else 0 end as is_flash_refund,
 30      row_number() over (partition by p.user_id order by p.pay_time) as purchase_seq,
 31      case
 32          when p.live_room_id is not null
 33               and (p.channel_id = 'live'
 34                    or p.pay_time <= p.live_end_time + interval 30 minute)
 35          then 'live'
 36          else p.channel_id
 37      end                                             as attributed_channel
 38  from pay p
 39  left join flash f
 40    on p.order_id = f.order_id
```

### dwm_refund_order_agg_1d(DWM)

**核对点**:**L3 统计日 `dt = refund_suc_date`(T14:分子按退款完成日归属)**;**L11 `days_pay_to_refund <= 14`(T2 窗口过滤实际生效处)**;L14-15 仅 REFUNDED 且到账时间非空。

源文件:`warehouse/dbt_project/models/dwm/dwm_refund_order_agg_1d.sql`(源/编译行号一致,页面溯源引用即编译行号)

```sql
  1  -- 售后域订单×日退款聚合:统计日=退款完成日(T14),14 天窗口分量(T2)
  2  select
  3      refund_suc_date                                as dt,
  4      order_id,
  5      user_id,
  6      channel_id,
  7      category_name,
  8      province,
  9      min(pay_date)                                  as pay_date,
 10      sum(refund_amt)                                as refund_amt_1d,
 11      sum(case when days_pay_to_refund <= 14 then refund_amt else 0 end) as refund_amt_14d_1d,
 12      count(*)                                       as refund_cnt
 13  from {{ ref('dwd_after_refund_detail') }}
 14  where refund_status = 'REFUNDED'
 15    and refund_suc_time is not null
 16  group by 1, 2, 3, 4, 5, 6
```

### dm_trade_stats_1d(DM)

**核对点**:L18 `sum(pay_amt)` 分母日汇总,L23 按 dt(=支付日)分组。注意 L28 的 `refund_rate` 是**当日口径**(T7 同名不同义),与本指标无关。

源文件:`warehouse/dbt_project/models/dm/dm_trade_stats_1d.sql`(源/编译行号一致,页面溯源引用即编译行号)

```sql
  1  -- 交易域日汇总:GMV 剔秒退(T1)、当日口径退款率(T7:与 APP 层 14 天口径同名不同义)、退款金额独立计算(T8 之一)
  2  with pay as (
  3      select * from {{ ref('dwm_trade_order_flag_1d') }}
  4  ),
  5  ref_daily as (
  6      select
  7          refund_suc_date  as dt,
  8          sum(refund_amt)  as refund_amt
  9      from {{ ref('dwd_after_refund_detail') }}
 10      where refund_status = 'REFUNDED'
 11        and refund_suc_time is not null
 12      group by 1
 13  ),
 14  agg as (
 15      select
 16          dt,
 17          sum(case when is_flash_refund = 0 then order_amt_cny else 0 end) as gmv,
 18          sum(pay_amt)             as pay_amt,
 19          count(distinct order_id) as pay_order_cnt,
 20          count(distinct user_id)  as pay_user_cnt,
 21          sum(is_flash_refund)     as flash_refund_order_cnt
 22      from pay
 23      group by dt
 24  )
 25  select
 26      a.dt,
 27      a.gmv,
 28      a.pay_amt,
 29      a.pay_order_cnt,
 30      a.pay_user_cnt,
 31      a.flash_refund_order_cnt,
 32      r.refund_amt,
 33      round(r.refund_amt / nullif(a.pay_amt, 0), 6) as refund_rate
 34  from agg a
 35  left join ref_daily r using (dt)
```

### dm_after_sale_stats_1d(DM)

**核对点**:L5 `sum(refund_amt_14d_1d)` 分子日汇总,L9 按 dt(=退款完成日)分组。

源文件:`warehouse/dbt_project/models/dm/dm_after_sale_stats_1d.sql`(源/编译行号一致,页面溯源引用即编译行号)

```sql
  1  -- 售后域日汇总:14 天窗口口径(T2)、统计日=退款完成日(T14)、退款金额独立计算(T8 之二,与交易域血缘指纹一致)
  2  select
  3      dt,
  4      sum(refund_amt_1d)       as refund_amt_total,
  5      sum(refund_amt_14d_1d)   as refund_amt_14d,
  6      sum(refund_cnt)          as refund_apply_cnt,
  7      count(distinct order_id) as refund_order_cnt
  8  from {{ ref('dwm_refund_order_agg_1d') }}
  9  group by dt
```

### app_business_overview_1d(APP)

**核对点**:**L10 `refund_amt_14d / nullif(pay_amt, 0)` 分子分母跨域拼接**;L24 `left join ... using (dt)`——两条支路各自的统计日在此对齐(跨期口径的来源)。

源文件:`warehouse/dbt_project/models/app/app_business_overview_1d.sql`(源/编译行号一致,页面溯源引用即编译行号)

```sql
  1  -- 业务大盘日宽表:跨四域收口;客单价按人(T5)、近14天退款率分子分母跨域拼接(T2)
  2  select
  3      t.dt,
  4      t.gmv,
  5      t.pay_amt,
  6      t.pay_order_cnt,
  7      t.pay_user_cnt,
  8      round(t.pay_amt / nullif(t.pay_user_cnt, 0), 2)   as atv,
  9      a.refund_amt_14d,
 10      round(a.refund_amt_14d / nullif(t.pay_amt, 0), 6) as refund_rate_14d,
 11      a.refund_amt_total,
 12      t.flash_refund_order_cnt,
 13      round(t.flash_refund_order_cnt * 1.0 / nullif(t.pay_order_cnt, 0), 6) as flash_refund_order_ratio,
 14      l.delivered_rate,
 15      l.avg_ship_hours,
 16      l.pickup_waybill_cnt,
 17      l.sign_waybill_cnt,
 18      u.new_user_cnt,
 19      u.new_user_gmv,
 20      u.repurchase_user_cnt,
 21      round(u.new_user_gmv / nullif(t.gmv, 0), 6)       as new_user_gmv_ratio,
 22      u.repurchase_rate
 23  from {{ ref('dm_trade_stats_1d') }} t
 24  left join {{ ref('dm_after_sale_stats_1d') }} a using (dt)
 25  left join {{ ref('dm_logistics_stats_1d') }}  l using (dt)
 26  left join {{ ref('dm_user_new_stats_1d') }}   u using (dt)
```

## 3. 页面口径卡 ↔ SQL 依据逐条对照

**页面定义**:近14天退款率=退款到账距支付≤14天且退款成功的退款金额/当日支付成功的支付金额（剔除测试单/风控单）

| # | 页面条款(原文) | SQL 依据 |
|---|---|---|
| 1 | 近14天按退款到账日距原支付日的日历天数≤14天计算，分子按退款完成日聚合 | dwm_refund_order_agg_1d L11(窗口过滤)+ dwd_after_refund_detail L25(天数定义) |
| 2 | 退款金额优先取实际打款金额，打款金额缺失时回退申请退款金额，单位分转元 | dwd_after_refund_detail L22(coalesce + /100) |
| 3 | 仅统计退款状态为已退款（REFUNDED）且退款到账时间非空的退款记录 | dwm_refund_order_agg_1d L14-15 |
| 4 | 分子与分母均剔除测试单和风控单 | dwd_trade_order_detail L42-43(分子分母共同上游) |
| 5 | 分母统计当日支付成功且支付时间非空的支付金额，单位分转元，按支付日聚合 | dwd_trade_pay_suc_detail L12(/100)→ dm_trade_stats_1d L18(sum)+L23(按支付日) |
| 6 | 订单状态限定为20、30、40、50、90（待补充业务注释） | dwd_trade_pay_suc_detail L17(where)+ dwd_after_refund_detail L37(join on) |
| 7 | 分子、分母统计日维度不同：分子按退款完成日，分母按支付日，最终按统计日左关联 | dwm_refund_order_agg_1d L3(dt=退款完成日)· dm_trade_stats_1d L23(dt=支付日)· app_business_overview_1d L24(using(dt) 左关联) |
| 8 | 同一业务单据存在多版本时，按采集时间取最新：订单按order_id，支付流水按order_id，退款申请按refund_id | dwd_trade_order_detail L5/L10 · dwd_trade_pay_suc_detail L6 · dwd_after_refund_detail L5 |
| 9 | 分母为0时指标返回空值（NULL），结果保留6位小数 | app_business_overview_1d L10(nullif(pay_amt,0) + round(...,6)) |

**页面 caveats**:
- 分子与分母统计日归属不同，同一统计日计算的退款率存在跨期口径，解读时需注意
- 退款成功金额历史接口原因存在NULL，指标使用申请退款金额兜底
- 订单状态20/30/40/50/90的业务含义未提供，当前为技术直译

## 4. 已知差异与说明(核对时预期会看到)

1. **页面 formula 是 LLM 归并的"等效示意表达"**,非任何一层的逐字 SQL——其中 `coalesce(u.is_test_account,0)=0` 引用的 `u` 别名在该子查询中无对应 FROM:这条剔除实际生效于 `dwd_trade_order_detail`(join 用户表 u),LLM 做跨层内联时保留了原别名。语义正确,写法上须知。
2. **条款「订单状态20/30/40/50/90(待补充业务注释)」**:dwd 层 schema.yml 未给 order_status 逐值注释(枚举含义在 ods sources.yml),生成器按规则降级为技术直译并标注——预期行为。
3. **分子分母窗口不对称是业务口径本身**:分子=当日完成的、距支付≤14 天的退款;分母=当日支付金额。大促日分母骤增会使当日比率骤降(见看板 08-08),这不是 bug,是口径。
4. 汇率折算(T12)与秒退剔除(T1)**不在**本指标路径:分母取自支付流水(已人民币),退款率不剔秒退。若在别处见到相关表述,属 GMV 口径。

---
*生成于 2026-08-25 · 依据 caliber/store/refund_rate_14d.json(页面数据源)与 models/ 源文件 · 血缘 JSON:`curl http://127.0.0.1:8612/api/lineage/app_business_overview_1d/refund_rate_14d`*

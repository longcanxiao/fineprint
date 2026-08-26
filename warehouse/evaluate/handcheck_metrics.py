#!/usr/bin/env python3
"""M2 验收:大盘 14 个指标与"手算 SQL"一致——绕开 DM/DWM,直接从 DWD 明细独立重算后对比 APP 宽表。"""
import sys
import os
from pathlib import Path

import duckdb

DB = Path(os.environ.get("METRICLENS_DB") or Path(__file__).resolve().parent.parent / "metriclens.duckdb")
DATES = ["2026-06-18", "2026-07-15", "2026-08-10"]
con = duckdb.connect(str(DB), read_only=True)

hand_sql = f"""
with pay as (
    select p.*,
        case when exists (select 1 from dwd.dwd_after_refund_detail r
                          where r.order_id = p.order_id
                            and datediff('second', p.pay_time, r.apply_time) <= 60) then 1 else 0 end as fl,
        row_number() over (partition by p.user_id order by p.pay_time) as seq,
        cast(min(p.pay_time) over (partition by p.user_id) as date)    as first_pay_date
    from dwd.dwd_trade_pay_suc_detail p
),
t as (
    select pay_date as dt,
        sum(case when fl = 0 then order_amt_cny else 0 end) as gmv,
        sum(pay_amt) as pay_amt,
        count(distinct order_id) as pay_order_cnt,
        count(distinct user_id)  as pay_user_cnt,
        sum(fl) as flash_cnt,
        count(distinct case when first_pay_date = pay_date then user_id end) as new_user_cnt,
        sum(case when first_pay_date = pay_date and fl = 0 then order_amt_cny else 0 end) as new_gmv,
        count(distinct case when seq >= 2 then user_id end) as rep_users
    from pay group by 1
),
r as (
    select refund_suc_date as dt,
        sum(refund_amt) as refund_amt_total,
        sum(case when days_pay_to_refund <= 14 then refund_amt else 0 end) as refund_amt_14d
    from dwd.dwd_after_refund_detail
    where refund_status = 'REFUNDED' and refund_suc_time is not null group by 1
),
l as (
    select cast(pickup_time as date) as dt,
        count(distinct waybill_id) as pw,
        count(distinct case when sign_time is not null then waybill_id end) as sw,
        avg(case when is_presale = 0 then (epoch(pickup_time) - epoch(pay_time)) / 3600.0 end) as ah
    from dwd.dwd_logistics_ship_detail where pickup_time is not null group by 1
),
lg as (  -- 渠道归因GMV·直播:独立重写归因规则(直播间支付 或 直播结束30分钟内支付)
    select pay_date as dt, sum(case when fl = 0 then order_amt_cny else 0 end) as live_gmv
    from pay
    where live_room_id is not null
      and (channel_id = 'live' or pay_time <= live_end_time + interval 30 minute)
    group by 1
)
select t.dt,
    round(t.gmv, 2)                                     as gmv,
    round(t.pay_amt, 2)                                 as pay_amt,
    t.pay_order_cnt, t.pay_user_cnt,
    round(t.pay_amt / nullif(t.pay_user_cnt, 0), 2)     as atv,
    round(r.refund_amt_14d, 2)                          as refund_amt_14d,
    round(r.refund_amt_14d / nullif(t.pay_amt, 0), 6)   as refund_rate_14d,
    round(r.refund_amt_total, 2)                        as refund_amt_total,
    round(t.flash_cnt * 1.0 / nullif(t.pay_order_cnt, 0), 6) as flash_refund_order_ratio,
    round(l.sw * 1.0 / nullif(l.pw, 0), 6)              as delivered_rate,
    round(l.ah, 2)                                      as avg_ship_hours,
    t.new_user_cnt,
    round(t.new_gmv / nullif(t.gmv, 0), 6)              as new_user_gmv_ratio,
    round(t.rep_users * 1.0 / nullif(t.pay_user_cnt, 0), 6) as repurchase_rate,
    round(lg.live_gmv, 2)                               as live_gmv
from t left join r using (dt) left join l using (dt) left join lg using (dt)
where t.dt in ({",".join("'"+d+"'" for d in DATES)})
order by t.dt
"""

app_sql = f"""
select b.dt, round(b.gmv,2), round(b.pay_amt,2), b.pay_order_cnt, b.pay_user_cnt, b.atv,
       round(b.refund_amt_14d,2), b.refund_rate_14d, round(b.refund_amt_total,2),
       b.flash_refund_order_ratio, b.delivered_rate, b.avg_ship_hours,
       b.new_user_cnt, b.new_user_gmv_ratio, b.repurchase_rate,
       round(c.live_gmv, 2) as live_gmv
from app.app_business_overview_1d b
left join (select dt, sum(gmv) live_gmv from app.app_channel_overview_1d
           where attributed_channel = 'live' group by dt) c using (dt)
where b.dt in ({",".join("'"+d+"'" for d in DATES)}) order by b.dt
"""

cols = ["dt", "gmv", "pay_amt", "pay_order_cnt", "pay_user_cnt", "atv", "refund_amt_14d",
        "refund_rate_14d", "refund_amt_total", "flash_refund_order_ratio", "delivered_rate",
        "avg_ship_hours", "new_user_cnt", "new_user_gmv_ratio", "repurchase_rate", "live_gmv"]
hand_rows = {str(r[0]): r for r in con.execute(hand_sql).fetchall()}
app_rows = {str(r[0]): r for r in con.execute(app_sql).fetchall()}
fails = 0
print("\n=== 指标手算对账(DWD 独立重算 vs APP 宽表)===\n")
# 按日期键全外对齐:任一侧缺日期即失败(杜绝 zip 静默跳过)
if set(hand_rows) != set(DATES) or set(app_rows) != set(DATES):
    print(f"  ✗ 日期覆盖不完整: 手算={sorted(hand_rows)} app={sorted(app_rows)} 期望={DATES}")
    fails += 1
for dt in DATES:
    hrow, arow = hand_rows.get(dt), app_rows.get(dt)
    if hrow is None or arow is None:
        continue
    bad = []
    for i, c in enumerate(cols[1:], 1):
        hv, av = hrow[i], arow[i]
        if hv is None and av is None:
            continue
        if hv is None or av is None or abs(float(hv) - float(av)) > max(0.011, abs(float(av)) * 1e-9):
            bad.append(f"{c}: hand={hv} app={av}")
    if bad:
        fails += 1
        print(f"  ✗ {dt}: " + "; ".join(bad))
    else:
        print(f"  ✓ {dt}  {len(cols)-1} 项指标全部一致  (GMV={hrow[1]:,.0f} 元, 退款率14d={hrow[7]*100:.2f}%, 直播归因GMV={hrow[15]:,.0f})")
con.close()
sys.exit(1 if fails else 0)

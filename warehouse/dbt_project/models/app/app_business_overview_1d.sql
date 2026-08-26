-- 业务大盘日宽表:跨四域收口;客单价按人(T5)、近14天退款率分子分母跨域拼接(T2)
select
    t.dt,
    t.gmv,
    t.pay_amt,
    t.pay_order_cnt,
    t.pay_user_cnt,
    round(t.pay_amt / nullif(t.pay_user_cnt, 0), 2)   as atv,
    a.refund_amt_14d,
    round(a.refund_amt_14d / nullif(t.pay_amt, 0), 6) as refund_rate_14d,
    a.refund_amt_total,
    t.flash_refund_order_cnt,
    round(t.flash_refund_order_cnt * 1.0 / nullif(t.pay_order_cnt, 0), 6) as flash_refund_order_ratio,
    l.delivered_rate,
    l.avg_ship_hours,
    l.pickup_waybill_cnt,
    l.sign_waybill_cnt,
    u.new_user_cnt,
    u.new_user_gmv,
    u.repurchase_user_cnt,
    round(u.new_user_gmv / nullif(t.gmv, 0), 6)       as new_user_gmv_ratio,
    u.repurchase_rate
from {{ ref('dm_trade_stats_1d') }} t
left join {{ ref('dm_after_sale_stats_1d') }} a using (dt)
left join {{ ref('dm_logistics_stats_1d') }}  l using (dt)
left join {{ ref('dm_user_new_stats_1d') }}   u using (dt)

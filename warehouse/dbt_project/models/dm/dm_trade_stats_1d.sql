-- 交易域日汇总:GMV 剔秒退(T1)、当日口径退款率(T7:与 APP 层 14 天口径同名不同义)、退款金额独立计算(T8 之一)
with pay as (
    select * from {{ ref('dwm_trade_order_flag_1d') }}
),
ref_daily as (
    select
        refund_suc_date  as dt,
        sum(refund_amt)  as refund_amt
    from {{ ref('dwd_after_refund_detail') }}
    where refund_status = 'REFUNDED'
      and refund_suc_time is not null
    group by 1
),
agg as (
    select
        dt,
        sum(case when is_flash_refund = 0 then order_amt_cny else 0 end) as gmv,
        sum(pay_amt)             as pay_amt,
        count(distinct order_id) as pay_order_cnt,
        count(distinct user_id)  as pay_user_cnt,
        sum(is_flash_refund)     as flash_refund_order_cnt
    from pay
    group by dt
)
select
    a.dt,
    a.gmv,
    a.pay_amt,
    a.pay_order_cnt,
    a.pay_user_cnt,
    a.flash_refund_order_cnt,
    r.refund_amt,
    round(r.refund_amt / nullif(a.pay_amt, 0), 6) as refund_rate
from agg a
left join ref_daily r using (dt)

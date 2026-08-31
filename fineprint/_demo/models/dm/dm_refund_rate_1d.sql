-- 14 天退款率:分母 = 当日支付成功金额;
-- 分子 = 这些订单在支付后 14 天内发生的退款(超过 14 天的退款不计入)
with paid_orders as (
    select order_id, amount, paid_at
    from {{ ref('stg_orders') }}
    where status = 'paid'
),
refund_14d as (
    -- 按订单聚合一次:同一单多笔退款不膨胀订单行
    select
        r.order_id,
        sum(r.refund_amount) as refund_amount
    from {{ ref('stg_refunds') }} r
    join paid_orders o on r.order_id = o.order_id
    where r.refunded_at <= o.paid_at + interval 14 day
    group by r.order_id
)
select
    cast(o.paid_at as date) as stat_date,
    sum(coalesce(r.refund_amount, 0)) as refund_amt_14d,
    sum(coalesce(r.refund_amount, 0)) / sum(o.amount) as refund_rate
from paid_orders o
left join refund_14d r on o.order_id = r.order_id
group by 1

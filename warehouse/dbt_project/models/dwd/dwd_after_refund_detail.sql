-- 售后域退款明细:申请多版本去重(T9)、打款金额兜底(T13)、跨域 join 补支付时间且 on 含状态限定(T2)
with ra as (
    select *
    from {{ source('ods', 'ods_refund_apply') }}
    qualify row_number() over (partition by refund_id order by binlog_ts desc) = 1
),
rp as (
    select * from {{ source('ods', 'ods_refund_payment') }}
)
select
    ra.refund_id,
    ra.order_id,
    ra.user_id,
    ra.refund_type,
    ra.refund_status,
    ra.refund_reason,
    ra.apply_time,
    rp.refund_suc_time,
    cast(rp.refund_suc_time as date)               as refund_suc_date,
    ra.refund_apply_amt_cent / 100.0               as refund_apply_amt,
    rp.refund_suc_amt_cent / 100.0                 as refund_suc_amt,
    coalesce(rp.refund_suc_amt_cent, ra.refund_apply_amt_cent) / 100.0 as refund_amt,
    o.pay_time,
    o.pay_date,
    datediff('day', o.pay_time, rp.refund_suc_time) as days_pay_to_refund,
    o.channel_id,
    o.live_room_id,
    o.category_id,
    o.category_name,
    o.province,
    o.order_amt_cny
from ra
left join rp
  on ra.refund_id = rp.refund_id
join {{ ref('dwd_trade_order_detail') }} o
  on ra.order_id = o.order_id
 and o.order_status in (20, 30, 40, 50, 90)

-- 支付成功订单明细:关联支付流水,单位分→元
with pf as (
    select *
    from {{ source('ods', 'ods_payment_flow') }}
    where pay_status = 'SUCCESS'
    qualify row_number() over (partition by order_id order by pay_time desc) = 1
)
select
    o.*,
    p.pay_id,
    p.pay_type,
    p.pay_amt_cent / 100.0 as pay_amt
from {{ ref('dwd_trade_order_detail') }} o
join pf p
  on o.order_id = p.order_id
where o.pay_time is not null
  and o.order_status in (20, 30, 40, 50, 90)

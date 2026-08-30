-- 退款清洗层:上游可能重复投递,按 refund_id 去重保留最新一条
select
    refund_id,
    order_id,
    refund_amount,
    refunded_at
from (
    select
        *,
        row_number() over (partition by refund_id order by refunded_at desc) as rn
    from {{ ref('raw_refunds') }}
) t
where rn = 1

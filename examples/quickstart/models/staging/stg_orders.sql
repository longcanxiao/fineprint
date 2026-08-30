-- 订单清洗层:剔除内部测试账号,只保留真实用户订单
select
    order_id,
    user_id,
    amount,
    status,
    paid_at
from {{ ref('raw_orders') }}
where is_test = 0

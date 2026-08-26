-- 用户域首购回溯(T3):新客口径的底表——历史首笔支付成功订单
select
    user_id,
    min(pay_time)                    as first_pay_time,
    cast(min(pay_time) as date)      as first_pay_date,
    arg_min(order_id, pay_time)      as first_order_id,
    arg_min(order_amt_cny, pay_time) as first_order_amt_cny
from {{ ref('dwd_trade_pay_suc_detail') }}
group by user_id

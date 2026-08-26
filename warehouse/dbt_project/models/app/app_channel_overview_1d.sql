-- 渠道日宽表:归因渠道(T11)×直播间×类目×省份
select
    dt,
    attributed_channel,
    live_room_id,
    category_name,
    province,
    sum(case when is_flash_refund = 0 then order_amt_cny else 0 end) as gmv,
    sum(pay_amt)             as pay_amt,
    count(distinct order_id) as pay_order_cnt,
    count(distinct user_id)  as pay_user_cnt,
    sum(is_flash_refund)     as flash_refund_order_cnt
from {{ ref('dwm_trade_order_flag_1d') }}
group by 1, 2, 3, 4, 5

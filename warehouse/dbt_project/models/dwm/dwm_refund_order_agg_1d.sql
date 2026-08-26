-- 售后域订单×日退款聚合:统计日=退款完成日(T14),14 天窗口分量(T2)
select
    refund_suc_date                                as dt,
    order_id,
    user_id,
    channel_id,
    category_name,
    province,
    min(pay_date)                                  as pay_date,
    sum(refund_amt)                                as refund_amt_1d,
    sum(case when days_pay_to_refund <= 14 then refund_amt else 0 end) as refund_amt_14d_1d,
    count(*)                                       as refund_cnt
from {{ ref('dwd_after_refund_detail') }}
where refund_status = 'REFUNDED'
  and refund_suc_time is not null
group by 1, 2, 3, 4, 5, 6

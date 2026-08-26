-- 售后域日汇总:14 天窗口口径(T2)、统计日=退款完成日(T14)、退款金额独立计算(T8 之二,与交易域血缘指纹一致)
select
    dt,
    sum(refund_amt_1d)       as refund_amt_total,
    sum(refund_amt_14d_1d)   as refund_amt_14d,
    sum(refund_cnt)          as refund_apply_cnt,
    count(distinct order_id) as refund_order_cnt
from {{ ref('dwm_refund_order_agg_1d') }}
group by dt

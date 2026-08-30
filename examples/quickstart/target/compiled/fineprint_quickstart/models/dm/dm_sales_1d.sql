-- 日粒度成交:GMV 只计支付成功订单(不含取消单;测试账号已在清洗层剔除)
select
    cast(paid_at as date) as stat_date,
    sum(amount) as gmv,
    count(distinct order_id) as order_cnt
from "fineprint_demo"."main"."stg_orders"
where status = 'paid'
group by 1
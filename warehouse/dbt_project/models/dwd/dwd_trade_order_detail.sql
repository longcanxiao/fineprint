-- 交易域订单明细:多版本去重(T9)、剔除测试/风控单、SCD2 汇率折算(T12)
with ord_latest as (
    select *
    from {{ source('ods', 'ods_order_info') }}
    qualify row_number() over (partition by order_id order by binlog_ts desc) = 1
),
usr as (
    select *
    from {{ source('ods', 'ods_user_info') }}
    qualify row_number() over (partition by user_id order by binlog_ts desc) = 1
),
rate as (
    select * from {{ source('ods', 'ods_exchange_rate') }}
)
select
    o.order_id,
    o.user_id,
    o.order_time,
    o.pay_time,
    cast(o.pay_time as date)                       as pay_date,
    o.order_status,
    o.channel_id,
    case when o.live_room_id > 0 then cast(o.live_room_id as bigint) end as live_room_id,
    o.live_end_time,
    o.category_id,
    o.category_name,
    o.province,
    o.currency,
    o.order_amt,
    round(o.order_amt * r.rate_to_cny, 2)          as order_amt_cny,
    o.item_cnt,
    o.is_presale,
    u.register_time,
    u.register_channel,
    o.binlog_ts
from ord_latest o
join usr u
  on o.user_id = u.user_id
left join rate r
  on o.currency = r.currency
 and cast(coalesce(o.pay_time, o.order_time) as date) between r.effective_start and r.effective_end
where coalesce(u.is_test_account, 0) = 0
  and o.is_risk_order = 0

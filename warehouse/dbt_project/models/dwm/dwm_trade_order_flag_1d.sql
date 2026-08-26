-- 交易域支付订单旗标:秒退(T1)/预售/购买序号(T10)/渠道延迟归因(T11)
with pay as (
    select * from {{ ref('dwd_trade_pay_suc_detail') }}
),
ra_latest as (
    select *
    from {{ source('ods', 'ods_refund_apply') }}
    qualify row_number() over (partition by refund_id order by binlog_ts desc) = 1
),
flash as (
    select distinct ra.order_id
    from ra_latest ra
    join pay p
      on ra.order_id = p.order_id
     and datediff('second', p.pay_time, ra.apply_time) <= 60
)
select
    p.pay_date                                      as dt,
    p.order_id,
    p.user_id,
    p.order_amt_cny,
    p.pay_amt,
    p.channel_id,
    p.live_room_id,
    p.category_id,
    p.category_name,
    p.province,
    p.is_presale,
    case when f.order_id is not null then 1 else 0 end as is_flash_refund,
    row_number() over (partition by p.user_id order by p.pay_time) as purchase_seq,
    case
        when p.live_room_id is not null
             and (p.channel_id = 'live'
                  or p.pay_time <= p.live_end_time + interval 30 minute)
        then 'live'
        else p.channel_id
    end                                             as attributed_channel
from pay p
left join flash f
  on p.order_id = f.order_id

-- 物流域发运明细:轨迹事件拉宽,签收/揽收等事件按运单去重取首个(T4)
with ev as (
    select
        waybill_id,
        min(case when event_type = 'SHIP'   then event_time end) as ship_time,
        min(case when event_type = 'PICKUP' then event_time end) as pickup_time,
        min(case when event_type = 'SIGN'   then event_time end) as sign_time,
        min(case when event_type = 'REJECT' then event_time end) as reject_time
    from {{ source('ods', 'ods_logistics_trace') }}
    group by waybill_id
)
select
    lo.waybill_id,
    lo.order_id,
    lo.carrier,
    lo.warehouse_code,
    lo.ship_op_time,
    ev.ship_time,
    ev.pickup_time,
    ev.sign_time,
    ev.reject_time,
    lo.logistics_status,
    o.pay_time,
    o.pay_date,
    o.is_presale,
    o.channel_id,
    o.category_name,
    o.province
from {{ source('ods', 'ods_logistics_order') }} lo
left join ev
  on lo.waybill_id = ev.waybill_id
join {{ ref('dwd_trade_order_detail') }} o
  on lo.order_id = o.order_id

-- 物流域日汇总(揽收日 cohort):妥投率分母=揽收单量(T4)、发货时长=支付→揽收且剔预售(T6)
select
    cast(pickup_time as date)  as dt,
    count(distinct waybill_id) as pickup_waybill_cnt,
    count(distinct case when sign_time is not null then waybill_id end) as sign_waybill_cnt,
    round(count(distinct case when sign_time is not null then waybill_id end)
          * 1.0 / nullif(count(distinct waybill_id), 0), 6)             as delivered_rate,
    round(avg(case when is_presale = 0
                   then (epoch(pickup_time) - epoch(pay_time)) / 3600.0 end), 2) as avg_ship_hours
from {{ ref('dwd_logistics_ship_detail') }}
where pickup_time is not null
group by 1

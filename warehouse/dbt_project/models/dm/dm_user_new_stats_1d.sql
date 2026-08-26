-- 用户域日汇总:新客=首笔支付在当日(T3,非注册口径)、复购率(T10)
with pay as (
    select * from {{ ref('dwm_trade_order_flag_1d') }}
),
fp as (
    select * from {{ ref('dwm_user_first_pay_1d') }}
)
select
    p.dt,
    count(distinct case when fp.first_pay_date = p.dt then p.user_id end) as new_user_cnt,
    sum(case when fp.first_pay_date = p.dt and p.is_flash_refund = 0
             then p.order_amt_cny else 0 end)                             as new_user_gmv,
    count(distinct p.user_id)                                             as pay_user_cnt,
    count(distinct case when p.purchase_seq >= 2 then p.user_id end)      as repurchase_user_cnt,
    round(count(distinct case when p.purchase_seq >= 2 then p.user_id end)
          * 1.0 / nullif(count(distinct p.user_id), 0), 6)                as repurchase_rate
from pay p
join fp
  on p.user_id = fp.user_id
group by p.dt

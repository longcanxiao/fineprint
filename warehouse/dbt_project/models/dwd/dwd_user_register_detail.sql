-- 用户域注册明细:剔除测试账号
with u as (
    select *
    from {{ source('ods', 'ods_user_info') }}
    qualify row_number() over (partition by user_id order by binlog_ts desc) = 1
)
select
    user_id,
    nick_name,
    gender,
    province,
    register_time,
    cast(register_time as date) as register_date,
    register_channel
from u
where coalesce(is_test_account, 0) = 0

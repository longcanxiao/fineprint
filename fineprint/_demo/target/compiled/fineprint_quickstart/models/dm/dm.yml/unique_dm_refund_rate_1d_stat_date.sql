
    
    

select
    stat_date as unique_field,
    count(*) as n_records

from "fineprint_demo"."main"."dm_refund_rate_1d"
where stat_date is not null
group by stat_date
having count(*) > 1



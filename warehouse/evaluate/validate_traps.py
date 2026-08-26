#!/usr/bin/env python3
"""M1 验收:14 道口径陷阱在数据上逐一可验证(每道陷阱给出证据数字)。"""
import sys
import os
from pathlib import Path

import duckdb

DB = Path(os.environ.get("METRICLENS_DB") or Path(__file__).resolve().parent.parent / "metriclens.duckdb")
con = duckdb.connect(str(DB), read_only=True)
def q(sql):
    return con.execute(sql).fetchone()

results = []


def check(tid, name, ok, evidence):
    results.append((tid, name, bool(ok), evidence))


# T1 秒退剔除:剔除前后 GMV 有感差异
g_ex, g_all, fcnt = q("""select sum(case when is_flash_refund=0 then order_amt_cny else 0 end),
                                sum(order_amt_cny), sum(is_flash_refund) from dwm.dwm_trade_order_flag_1d""")
d1 = (g_all - g_ex) / g_all * 100
check("T1", "GMV 剔秒退", fcnt > 3000 and d1 > 0.3, f"秒退单 {fcnt:,} 笔,剔除使 GMV 下降 {d1:.2f}%")

# T2 14 天窗口:边界样本充足,窗口内外金额可分
a14, atot = q("select sum(refund_amt_14d), sum(refund_amt_total) from dm.dm_after_sale_stats_1d")
b13, b14, b15 = [q(f"""select count(*) from dwd.dwd_after_refund_detail
                       where refund_status='REFUNDED' and days_pay_to_refund={d}""")[0] for d in (13, 14, 15)]
check("T2", "退款 14 天窗口", a14 < atot * 0.98 and min(b13, b14, b15) > 300,
      f"14天内退款占 {a14/atot*100:.1f}%;第13/14/15天边界样本 {b13}/{b14}/{b15} 笔")

# T3 新客=首购口径,与注册口径显著不同
nc = q("select sum(new_user_cnt) from dm.dm_user_new_stats_1d")[0]
reg = q("""select count(distinct p.user_id) from dwm.dwm_trade_order_flag_1d p
           join dwd.dwd_user_register_detail u using(user_id)
           where u.register_date = p.dt""")[0]
check("T3", "新客首购口径", abs(nc - reg) / nc > 0.2, f"首购口径新客 {nc:,} vs 当日注册口径 {reg:,}")

# T4 妥投率:分母揽收≠发货;重复签收事件需去重
dup = q("""select count(*) from (select waybill_id from ods.ods_logistics_trace
           where event_type='SIGN' group by waybill_id having count(*)>1)""")[0]
shipped, picked, signed = q("""select count(distinct lo.waybill_id),
        count(distinct case when t.pickup_time is not null then lo.waybill_id end),
        count(distinct case when t.sign_time is not null then lo.waybill_id end)
        from ods.ods_logistics_order lo left join
        (select waybill_id, min(case when event_type='PICKUP' then event_time end) pickup_time,
                min(case when event_type='SIGN' then event_time end) sign_time
         from ods.ods_logistics_trace group by 1) t using(waybill_id)""")
check("T4", "妥投率分母口径", dup > 5000 and picked < shipped,
      f"重复签收 {dup:,} 单;发货 {shipped:,} vs 揽收 {picked:,} vs 签收 {signed:,},两种分母结果不同")

# T5 客单价按人 vs 按单
pa, pu, po = q("select sum(pay_amt), count(distinct user_id), count(distinct order_id) from dwm.dwm_trade_order_flag_1d")
check("T5", "客单价分母歧义", abs(pa/pu - pa/po) / (pa/po) > 0.05, f"按人 {pa/pu:.1f} 元 vs 按单 {pa/po:.1f} 元")

# T6 发货时长:剔预售 + 揽收锚点
h_ex, h_in, h_op = q("""select
    avg(case when is_presale=0 then (epoch(pickup_time)-epoch(pay_time))/3600.0 end),
    avg((epoch(pickup_time)-epoch(pay_time))/3600.0),
    avg(case when is_presale=0 then (epoch(ship_op_time)-epoch(pay_time))/3600.0 end)
    from dwd.dwd_logistics_ship_detail where pickup_time is not null""")
check("T6", "发货时长口径", (h_in - h_ex) / h_ex > 0.15 and (h_ex - h_op) > 1,
      f"剔预售 {h_ex:.1f}h vs 含预售 {h_in:.1f}h;揽收锚点比发货操作锚点多 {h_ex-h_op:.1f}h")

# T7 同名不同义:两个 refund_rate
diff7 = q("""select avg(abs(t.refund_rate - a.refund_rate_14d)/nullif(a.refund_rate_14d,0))
             from dm.dm_trade_stats_1d t join app.app_business_overview_1d a using(dt)""")[0]
check("T7", "退款率同名不同义", diff7 > 0.05, f"当日口径 vs 14天口径日均相对差异 {diff7*100:.1f}%")

# T8 重复建设:两域 refund_amt 完全一致
mx8, cnt8 = q("""select max(abs(coalesce(t.refund_amt,0) - coalesce(a.refund_amt_total,0))), count(*)
                 from dm.dm_trade_stats_1d t join dm.dm_after_sale_stats_1d a using(dt)""")
check("T8", "退款金额重复建设", mx8 < 0.01 and cnt8 > 80, f"{cnt8} 天两域退款金额最大差异 {mx8:.4f} 元(指纹一致)")

# T9 多版本去重
raw, uniq = q("select count(*), count(distinct order_id) from ods.ods_order_info")
dwd_rows, dwd_uniq = q("select count(*), count(distinct order_id) from dwd.dwd_trade_order_detail")
check("T9", "binlog 多版本去重", raw > uniq * 1.5 and dwd_rows == dwd_uniq,
      f"ODS {raw:,} 行/{uniq:,} 单(×{raw/uniq:.2f});DWD 去重后一单一行")

# T10 复购率窗口序号
rep, tot = q("""select count(distinct case when purchase_seq>=2 then user_id end),
                count(distinct user_id) from dwm.dwm_trade_order_flag_1d""")
check("T10", "复购率窗口序号", rep > 1000 and 0.05 < rep/tot < 0.95, f"复购用户 {rep:,}/{tot:,} = {rep/tot*100:.1f}%")

# T11 延迟归因:归因口径 GMV > 直接渠道口径
attr, naive = q("""select sum(case when attributed_channel='live' then order_amt_cny end),
                          sum(case when channel_id='live' then order_amt_cny end)
                   from dwm.dwm_trade_order_flag_1d""")
check("T11", "直播延迟归因", (attr - naive) / naive > 0.02,
      f"归因口径直播 GMV 比支付端口径高 {(attr-naive)/naive*100:.1f}%")

# T12 SCD2 汇率:多段汇率均有订单,折算改变 GMV
seg12 = q("""select count(distinct r.rate_to_cny) from dwd.dwd_trade_order_detail o
             join ods.ods_exchange_rate r on o.currency=r.currency
              and o.pay_date between r.effective_start and r.effective_end
             where o.currency='USD' and o.pay_time is not null""")[0]
usd_raw, usd_cny = q("""select sum(order_amt), sum(order_amt_cny) from dwd.dwd_trade_order_detail
                        where currency='USD' and pay_time is not null""")
check("T12", "SCD2 汇率折算", seg12 >= 3 and usd_cny > usd_raw * 5,
      f"USD 订单跨 {seg12} 段汇率;原币 {usd_raw:,.0f} → 人民币 {usd_cny:,.0f}")

# T13 打款金额兜底
nulls, coal, suconly = q("""select count(case when refund_suc_amt is null then 1 end),
        sum(refund_amt), sum(refund_suc_amt)
        from dwd.dwd_after_refund_detail where refund_status='REFUNDED'""")
check("T13", "打款缺失兜底", nulls > 1000 and coal > suconly * 1.01,
      f"打款金额缺失 {nulls:,} 笔;coalesce 兜底后总额多 {(coal/suconly-1)*100:.1f}%")

# T14 统计日归属:打款跨日
cross, tot14 = q("""select count(case when refund_suc_date > cast(apply_time as date) then 1 end), count(*)
                    from dwd.dwd_after_refund_detail where refund_status='REFUNDED'""")
check("T14", "统计日跨日归属", cross / tot14 > 0.3, f"申请→打款跨日 {cross:,}/{tot14:,} = {cross/tot14*100:.0f}%")

ok_n = sum(1 for r in results if r[2])
print(f"\n=== 口径陷阱数据验证 {ok_n}/14 ===\n")
for tid, name, ok, ev in results:
    print(f"  {'✓' if ok else '✗'} {tid:<4} {name:<12} {ev}")
con.close()
sys.exit(0 if ok_n == 14 else 1)

#!/usr/bin/env python3
"""MetricLens 数据模拟器:场景注入式生成 90 天电商四域 ODS 数据(写入 DuckDB ods schema)。

先按真实分布生成基底数据,再按 scenarios.yml 定向注入 14 道口径陷阱的边界样本。
全程 numpy 向量化,固定 seed 可复现;一键重建:python generate.py
"""
import json
import time
from datetime import datetime
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml
from faker import Faker

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("METRICLENS_DB") or ROOT.parent / "metriclens.duckdb")


def td_h(rng, lo, hi, n):
    return pd.Series(pd.to_timedelta(np.round(rng.uniform(lo, hi, n) * 3600).astype("int64"), unit="s"))


def main():
    t0 = time.time()
    cfg = yaml.safe_load((ROOT / "scenarios.yml").read_text())
    rng = np.random.default_rng(cfg["seed"])
    Faker.seed(cfg["seed"])
    fake = Faker("zh_CN")

    end = pd.Timestamp(cfg["end_date"]) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    days = cfg["days"]
    start = pd.Timestamp(cfg["end_date"]) - pd.Timedelta(days=days - 1)
    n_users = cfg["users"]

    # ---------------- 汇率 SCD2 (T12) ----------------
    rate_rows = [("CNY", 1.0, "2020-01-01", "2099-12-31")]
    for seg in cfg["exchange_rate_usd"]:
        rate_rows.append(("USD", seg["rate"], seg["start"], seg["end"]))
    df_rate = pd.DataFrame(rate_rows, columns=["currency", "rate_to_cny", "effective_start", "effective_end"])
    df_rate["effective_start"] = pd.to_datetime(df_rate["effective_start"]).dt.date
    df_rate["effective_end"] = pd.to_datetime(df_rate["effective_end"]).dt.date

    def usd_rate_at(ts: pd.Series) -> np.ndarray:
        d = ts.dt.date.astype("object")
        out = np.full(len(ts), np.nan)
        for seg in cfg["exchange_rate_usd"]:
            s = pd.Timestamp(seg["start"]).date()
            e = pd.Timestamp(seg["end"]).date()
            m = (d >= s) & (d <= e)
            out[np.asarray(m)] = seg["rate"]
        return out

    # ---------------- 用户基底 ----------------
    user_id = np.arange(100001, 100001 + n_users, dtype="int64")
    fam = np.array([fake.last_name() for _ in range(200)])
    giv = np.array([fake.first_name() for _ in range(400)])
    nick = np.char.add(fam[rng.integers(0, len(fam), n_users)], giv[rng.integers(0, len(giv), n_users)])
    gender = rng.choice(np.array(["F", "M", "U"]), n_users, p=[0.52, 0.44, 0.04])
    pnames = np.array([p[0] for p in cfg["provinces"]])
    pw = np.array([float(p[1]) for p in cfg["provinces"]])
    pw /= pw.sum()
    u_prov = pnames[rng.choice(len(pnames), n_users, p=pw)]
    is_test_account = (rng.random(n_users) < cfg["test_account_ratio"]).astype("int8")
    reg_channel = rng.choice(np.array(["app", "h5", "live", "ground"]), n_users, p=[0.5, 0.25, 0.15, 0.1])

    # ---------------- 订单基底 ----------------
    day_idx = np.arange(days)
    dts = pd.date_range(start, periods=days, freq="D")
    dow = dts.weekday.to_numpy()
    vol = cfg["orders_per_day"] * (1 + 0.12 * np.isin(dow, [5, 6])) * (1 + 0.0015 * day_idx)
    for d, f in cfg["promo_days"].items():
        i = (pd.Timestamp(d) - start).days
        if 0 <= i < days:
            vol[i] *= f
    counts = rng.poisson(vol)
    n_orders = int(counts.sum())
    order_day = np.repeat(day_idx, counts)

    hp = np.array(cfg["hour_weights"], float)
    hp /= hp.sum()
    order_time = (
        pd.Series(pd.Timestamp(start)).repeat(n_orders).reset_index(drop=True)
        + pd.to_timedelta(order_day, unit="D")
        + pd.to_timedelta(rng.choice(24, n_orders, p=hp), unit="h")
        + pd.to_timedelta(rng.integers(0, 3600, n_orders), unit="s")
    )

    order_id = np.arange(5_000_001, 5_000_001 + n_orders, dtype="int64")
    w = rng.lognormal(0, 1.25, n_users)
    o_user = user_id[rng.choice(n_users, n_orders, p=w / w.sum())]

    # 渠道与直播场景 (T11)
    u01 = rng.random(n_orders)
    ch = cfg["channel"]
    live_scene = u01 < ch["live_scene"]
    h5 = (u01 >= ch["live_scene"]) & (u01 < ch["live_scene"] + ch["h5"])
    channel_id = np.where(live_scene, "live", np.where(h5, "h5", "app"))
    pay_in_live = live_scene & (rng.random(n_orders) < cfg["live_pay_in_live"])
    channel_id = np.where(live_scene & ~pay_in_live, "app", channel_id)  # 下单在直播间、跳 App 支付
    live_room_id = np.where(live_scene, rng.integers(8001, 8121, n_orders), 0)
    live_end_time = order_time + pd.to_timedelta(np.round(rng.uniform(5, 120, n_orders) * 60).astype("int64"), unit="s")
    live_end_time = live_end_time.where(pd.Series(live_scene), pd.NaT)

    # 类目与金额
    cats = cfg["categories"]
    cw = np.array([c["w"] for c in cats], float)
    cw /= cw.sum()
    cat_idx = rng.choice(len(cats), n_orders, p=cw)
    cat_names = np.array([c["name"] for c in cats])
    cat_mu = np.array([c["mu"] for c in cats])
    amt_cny = np.round(np.exp(rng.normal(cat_mu[cat_idx], 0.55)), 2).clip(5, 80000)

    o_prov_map = pd.Series(u_prov, index=user_id)
    o_prov = o_prov_map.loc[o_user].to_numpy()

    is_risk = (rng.random(n_orders) < cfg["risk_order_ratio"]).astype("int8")
    is_presale = (rng.random(n_orders) < cfg["presale_ratio"]).astype("int8")

    # 支付 (含 T11 延迟归因样本)
    paid = rng.random(n_orders) < cfg["pay_rate"]
    delay_s = np.round(rng.exponential(18 * 60, n_orders)).clip(30, 48 * 3600).astype("int64")
    pay_time = order_time + pd.to_timedelta(delay_s, unit="s")
    delayed_grp = live_scene & ~pay_in_live
    within = rng.random(n_orders) < cfg["live_delayed_within_30m"]
    off_in = pd.to_timedelta(np.round(rng.uniform(60, 29 * 60, n_orders)).astype("int64"), unit="s")
    off_out = pd.to_timedelta(np.round(rng.uniform(31 * 60, 6 * 3600, n_orders)).astype("int64"), unit="s")
    pay_time = pay_time.mask(pd.Series(delayed_grp & within), live_end_time + off_in)
    pay_time = pay_time.mask(pd.Series(delayed_grp & ~within), live_end_time + off_out)
    paid &= (pay_time <= end).to_numpy()
    pay_time = pay_time.where(pd.Series(paid), pd.NaT)

    # 币种 (T12): 基础 5%,汇率切换日加密到 15%
    usd = rng.random(n_orders) < cfg["usd_ratio"]
    switch_days = [pd.Timestamp(s["start"]).date() for s in cfg["exchange_rate_usd"][1:]]
    on_switch = np.isin(order_time.dt.date.to_numpy(), switch_days)
    usd |= on_switch & (rng.random(n_orders) < cfg["usd_ratio_on_switch_day"])
    rate_ref = pay_time.fillna(order_time)
    r_usd = usd_rate_at(rate_ref)
    currency = np.where(usd, "USD", "CNY")
    order_amt = np.where(usd, np.round(amt_cny / np.where(np.isnan(r_usd), 7.1, r_usd), 2), amt_cny)
    amt_cny_eff = np.where(usd, order_amt * np.where(np.isnan(r_usd), 7.1, r_usd), order_amt)

    # ---------------- 订单明细 (items) ----------------
    n_items = rng.choice(np.array([1, 2, 3]), n_orders, p=[0.7, 0.22, 0.08])
    total_items = int(n_items.sum())
    it_parent = np.repeat(np.arange(n_orders), n_items)
    share = rng.random(total_items) + 0.15
    share_sum = np.bincount(it_parent, weights=share, minlength=n_orders)
    it_amt = np.round(order_amt[it_parent] * share / share_sum[it_parent], 2)
    it_qty = rng.choice(np.array([1, 1, 1, 2]), total_items)
    df_item = pd.DataFrame(
        {
            "item_id": np.arange(90_000_001, 90_000_001 + total_items, dtype="int64"),
            "order_id": order_id[it_parent],
            "sku_id": rng.integers(10001, 99999, total_items),
            "sku_name": np.char.add(np.char.add(cat_names[cat_idx][it_parent].astype("U16"), "-SKU"),
                                     rng.integers(100, 999, total_items).astype("U4")),
            "category_id": cat_idx[it_parent] + 1,
            "category_name": cat_names[cat_idx][it_parent],
            "item_amt": it_amt,
            "quantity": it_qty,
            "currency": currency[it_parent],
            "binlog_ts": (order_time.iloc[it_parent].reset_index(drop=True)
                          + pd.to_timedelta(rng.integers(1, 30, total_items), unit="s")),
        }
    )
    order_amt = np.bincount(it_parent, weights=it_amt, minlength=n_orders).round(2)  # 与 item 精确一致
    amt_cny_eff = np.where(usd, np.round(order_amt * np.where(np.isnan(r_usd), 7.1, r_usd), 2), order_amt)

    # ---------------- 退款场景注入 (T1/T2/T13/T14) ----------------
    rc = cfg["refund"]
    p_flash = np.where(live_scene, rc["flash_base"] + rc["flash_live_boost"], rc["flash_base"])
    u2 = rng.random(n_orders)
    b13, b14, b15 = rc["boundary_13"], rc["boundary_14"], rc["boundary_15"]
    cum_f = p_flash
    is_flash = paid & (u2 < cum_f)
    is_b13 = paid & (u2 >= cum_f) & (u2 < cum_f + b13)
    is_b14 = paid & (u2 >= cum_f + b13) & (u2 < cum_f + b13 + b14)
    is_b15 = paid & (u2 >= cum_f + b13 + b14) & (u2 < cum_f + b13 + b14 + b15)
    is_norm = paid & (u2 >= cum_f + b13 + b14 + b15) & (u2 < cum_f + b13 + b14 + b15 + rc["normal"])
    has_refund = is_flash | is_b13 | is_b14 | is_b15 | is_norm
    ridx = np.where(has_refund)[0]
    n_ref = len(ridx)

    r_pay = pay_time.iloc[ridx].reset_index(drop=True)
    kind = np.select(
        [is_flash[ridx], is_b13[ridx], is_b14[ridx], is_b15[ridx]], ["flash", "b13", "b14", "b15"], "norm"
    )
    # 成功打款时刻:边界样本按 [13/14/15] 个日历日精确落位(T2);normal 对数正态;flash 分钟级
    suc = r_pay.copy()
    m = kind == "flash"
    suc[m] = r_pay[m] + td_h(rng, 0.2, 4, int(m.sum()))
    for k, dcnt in [("b13", 13), ("b14", 14), ("b15", 15)]:
        m = kind == k
        n = int(m.sum())
        suc[m] = (r_pay[m].dt.normalize() + pd.Timedelta(days=dcnt)
                  + pd.to_timedelta(rng.integers(8 * 3600, 23 * 3600, n), unit="s"))
    m = kind == "norm"
    n = int(m.sum())
    suc[m] = r_pay[m] + pd.to_timedelta(
        np.round(np.exp(rng.normal(1.35, 0.9, n)) * 86400).clip(3600, 45 * 86400).astype("int64"), unit="s")
    # 申请时刻 = 打款 - 延迟(T14 跨日);flash 的申请必须在支付后 60s 内(T1)
    lag = td_h(rng, rc["payout_delay_hours"][0], rc["payout_delay_hours"][1], n_ref)
    apply_t = suc - lag
    fl = kind == "flash"
    apply_t[fl] = r_pay[fl] + pd.to_timedelta(rng.integers(5, 60, int(fl.sum())), unit="s")
    apply_t = apply_t.clip(lower=r_pay + pd.Timedelta(seconds=90)).where(pd.Series(~fl), apply_t)
    suc = suc.clip(lower=apply_t + pd.Timedelta(minutes=10))

    partial = rng.random(n_ref) < rc["partial_ratio"]
    ratio = np.where(partial, rng.uniform(0.2, 0.8, n_ref), 1.0)
    ratio = np.where(fl, np.where(rng.random(n_ref) < 0.9, 1.0, ratio), ratio)  # 秒退基本全额
    r_amt_cny = np.round(amt_cny_eff[ridx] * ratio, 2)
    rejected = (rng.random(n_ref) < rc["rejected_ratio"]) & ~fl
    censored = (suc > end).to_numpy()  # 打款晚于快照期,仍处申请中
    refunded = ~rejected & ~censored
    refund_id = np.arange(7_000_001, 7_000_001 + n_ref, dtype="int64")
    r_type = np.where(fl | (rng.random(n_ref) < 0.55), "ONLY_REFUND", "RETURN_REFUND")

    full_refund = refunded & (ratio >= 0.999)

    # ---------------- 物流 (T4/T6) ----------------
    lg = cfg["logistics"]
    flash_full = np.zeros(n_orders, bool)
    flash_full[ridx[fl & full_refund]] = True
    ships = paid & ~flash_full & (is_risk == 0)
    sidx = np.where(ships)[0]
    n_ship = len(sidx)
    s_pay = pay_time.iloc[sidx].reset_index(drop=True)
    ship_delay = td_h(rng, lg["ship_hours"][0], lg["ship_hours"][1], n_ship)
    pre = is_presale[sidx] == 1
    ship_delay[pre] = pd.to_timedelta(
        np.round(rng.uniform(lg["presale_ship_days"][0], lg["presale_ship_days"][1], int(pre.sum())) * 86400
                 ).astype("int64"), unit="s").to_numpy()
    ship_op = s_pay + ship_delay
    pickup = ship_op + td_h(rng, lg["pickup_hours"][0], lg["pickup_hours"][1], n_ship)
    sign = pickup + td_h(rng, lg["sign_hours"][0], lg["sign_hours"][1], n_ship)
    rejected_pkg = rng.random(n_ship) < lg["reject_ratio"]
    waybill = np.arange(60_000_001, 60_000_001 + n_ship, dtype="int64")
    carriers = rng.choice(np.array(["顺丰", "中通", "圆通", "韵达", "京东物流"]), n_ship, p=[0.2, 0.3, 0.2, 0.18, 0.12])

    def in_win(s):
        return s <= end
    df_lo = pd.DataFrame(
        {
            "waybill_id": waybill,
            "order_id": order_id[sidx],
            "carrier": carriers,
            "warehouse_code": np.char.add("WH", rng.integers(1, 9, n_ship).astype("U1")),
            "ship_op_time": ship_op,
            "logistics_status": np.where(~in_win(ship_op), "TO_SHIP",
                                np.where(~in_win(pickup), "SHIPPED",
                                np.where(rejected_pkg & in_win(sign), "REJECTED",
                                np.where(in_win(sign), "SIGNED", "IN_TRANSIT")))),
            "binlog_ts": ship_op + pd.Timedelta(seconds=30),
        }
    )
    df_lo = df_lo[in_win(ship_op)].reset_index(drop=True)

    ev = []
    def add_ev(mask, etype, ts):
        m = mask & in_win(ts)
        ev.append(pd.DataFrame({"waybill_id": waybill[m], "event_type": etype,
                                 "event_time": ts[m].reset_index(drop=True) if isinstance(ts, pd.Series) else ts[m]}))
    base_m = in_win(ship_op).to_numpy()
    add_ev(base_m, "SHIP", ship_op + pd.to_timedelta(rng.integers(0, 7200, n_ship), unit="s"))
    add_ev(base_m, "PICKUP", pickup)
    add_ev(base_m, "TRANSPORT", pickup + td_h(rng, 4, 24, n_ship))
    sgn_m = base_m & ~rejected_pkg
    add_ev(sgn_m, "DELIVERY", sign - td_h(rng, 1, 4, n_ship))
    add_ev(sgn_m, "SIGN", sign)
    dup = sgn_m & (rng.random(n_ship) < lg["dup_sign_ratio"])
    add_ev(dup, "SIGN", sign + pd.to_timedelta(rng.integers(60, 1800, n_ship), unit="s"))
    add_ev(base_m & rejected_pkg, "REJECT", pickup + td_h(rng, 24, 96, n_ship))
    df_trace = pd.concat(ev, ignore_index=True)
    df_trace["trace_id"] = np.arange(1, len(df_trace) + 1, dtype="int64")

    # ---------------- 订单终态与多版本 (T9) ----------------
    sign_at = pd.Series(pd.NaT, index=np.arange(n_orders))
    sign_ok = np.zeros(n_orders, bool)
    tmp = sgn_m & in_win(sign).to_numpy()
    sign_at.iloc[sidx[tmp]] = sign[tmp].to_numpy()
    sign_ok[sidx[tmp]] = True
    shipped_ok = np.zeros(n_orders, bool)
    shipped_ok[sidx[base_m]] = True

    full_ref_o = np.zeros(n_orders, bool)
    full_ref_o[ridx[full_refund]] = True
    age_d = (end - order_time).dt.total_seconds().to_numpy() / 86400
    status = np.where(~paid, np.where(age_d > 2, 60, 10),
             np.where(full_ref_o, 90,
             np.where(sign_ok & (age_d > 12), 50,
             np.where(sign_ok, 40, np.where(shipped_ok, 30, 20))))).astype("int16")

    def order_snapshot(idx, st, ptime, blog):
        return pd.DataFrame(
            {
                "order_id": order_id[idx], "user_id": o_user[idx],
                "order_time": order_time.iloc[idx].to_numpy(),
                "pay_time": ptime, "order_status": st,
                "channel_id": channel_id[idx], "live_room_id": np.where(live_room_id[idx] > 0, live_room_id[idx], np.nan),
                "live_end_time": live_end_time.iloc[idx].to_numpy(),
                "category_id": cat_idx[idx] + 1, "category_name": cat_names[cat_idx[idx]],
                "province": o_prov[idx], "order_amt": order_amt[idx], "currency": currency[idx],
                "item_cnt": n_items[idx], "is_presale": is_presale[idx], "is_risk_order": is_risk[idx],
                "binlog_ts": blog,
            }
        )

    ov = cfg["order_versions"]
    all_idx = np.arange(n_orders)
    final_blog = pd.concat([order_time, pay_time, sign_at.set_axis(order_time.index)], axis=1).max(axis=1) \
        + pd.to_timedelta(rng.integers(60, 3600, n_orders), unit="s")
    snaps = [order_snapshot(all_idx, status, pay_time.to_numpy(), final_blog.to_numpy())]
    m1 = rng.random(n_orders) < ov["created_ratio"]
    snaps.append(order_snapshot(all_idx[m1], np.full(int(m1.sum()), 10, dtype="int16"),
                                np.full(int(m1.sum()), np.datetime64("NaT", "ns")),
                                (order_time[m1] + pd.to_timedelta(rng.integers(1, 30, int(m1.sum())), unit="s")).to_numpy()))
    m2 = paid & (rng.random(n_orders) < ov["paid_ratio"])
    snaps.append(order_snapshot(all_idx[m2], np.full(int(m2.sum()), 20, dtype="int16"),
                                pay_time[m2].to_numpy(),
                                (pay_time[m2] + pd.to_timedelta(rng.integers(1, 60, int(m2.sum())), unit="s")).to_numpy()))
    df_order = pd.concat(snaps, ignore_index=True)

    # ---------------- 支付流水 (单位:分, T13 同族) ----------------
    pidx = np.where(paid)[0]
    df_payflow = pd.DataFrame(
        {
            "pay_id": np.arange(30_000_001, 30_000_001 + len(pidx), dtype="int64"),
            "order_id": order_id[pidx], "user_id": o_user[pidx],
            "pay_amt_cent": np.round(amt_cny_eff[pidx] * 100).astype("int64"),
            "currency_pay": "CNY",
            "pay_type": rng.choice(np.array(["ALIPAY", "WXPAY", "CARD", "BALANCE"]), len(pidx), p=[0.42, 0.42, 0.1, 0.06]),
            "pay_status": "SUCCESS",
            "pay_time": pay_time.iloc[pidx].to_numpy(),
            "binlog_ts": (pay_time.iloc[pidx] + pd.to_timedelta(rng.integers(1, 20, len(pidx)), unit="s")).to_numpy(),
        }
    )
    fail_n = int(n_orders * 0.05)
    fi = rng.choice(n_orders, fail_n, replace=False)
    df_fail = pd.DataFrame(
        {
            "pay_id": np.arange(38_000_001, 38_000_001 + fail_n, dtype="int64"),
            "order_id": order_id[fi], "user_id": o_user[fi],
            "pay_amt_cent": np.round(amt_cny_eff[fi] * 100).astype("int64"),
            "currency_pay": "CNY",
            "pay_type": rng.choice(np.array(["ALIPAY", "WXPAY"]), fail_n),
            "pay_status": "FAIL",
            "pay_time": (order_time.iloc[fi] + pd.to_timedelta(rng.integers(10, 600, fail_n), unit="s")).to_numpy(),
            "binlog_ts": (order_time.iloc[fi] + pd.to_timedelta(rng.integers(11, 620, fail_n), unit="s")).to_numpy(),
        }
    )
    df_payflow = pd.concat([df_payflow, df_fail], ignore_index=True)

    # ---------------- 退款申请(多版本)与打款 ----------------
    r_status = np.where(rejected, "REJECTED", np.where(censored, "APPLY", "REFUNDED"))
    reasons = rng.choice(np.array(["七天无理由", "质量问题", "拍错/不想要", "发货太慢", "价保退差", "其他"]),
                         n_ref, p=[0.32, 0.2, 0.24, 0.12, 0.06, 0.06])
    apply_cent = np.round(r_amt_cny * 100).astype("int64")
    v1 = pd.DataFrame(
        {
            "refund_id": refund_id, "order_id": order_id[ridx], "user_id": o_user[ridx],
            "refund_type": r_type, "refund_status": "APPLY", "refund_reason": reasons,
            "refund_apply_amt_cent": apply_cent, "apply_time": apply_t.to_numpy(),
            "binlog_ts": (apply_t + pd.to_timedelta(rng.integers(1, 30, n_ref), unit="s")).to_numpy(),
        }
    )
    fin_m = r_status != "APPLY"
    fin_blog = pd.Series(np.where(rejected, (apply_t + td_h(rng, 1, 48, n_ref)).to_numpy(), suc.to_numpy()))
    v2 = v1[fin_m].copy()
    v2["refund_status"] = r_status[fin_m]
    v2["binlog_ts"] = (fin_blog[fin_m] + pd.to_timedelta(rng.integers(1, 30, int(fin_m.sum())), unit="s")).to_numpy()
    df_refund_apply = pd.concat([v1, v2], ignore_index=True)

    rf_m = r_status == "REFUNDED"
    n_rp = int(rf_m.sum())
    suc_cent = apply_cent[rf_m].astype("float64")
    null_m = rng.random(n_rp) < rc["null_suc_amt_ratio"]
    suc_cent[null_m] = np.nan  # T13 打款金额缺失
    df_refund_pay = pd.DataFrame(
        {
            "refund_pay_id": np.arange(80_000_001, 80_000_001 + n_rp, dtype="int64"),
            "refund_id": refund_id[rf_m], "order_id": order_id[ridx][rf_m],
            "refund_suc_amt_cent": suc_cent,
            "payout_channel": rng.choice(np.array(["ALIPAY", "WXPAY", "CARD"]), n_rp, p=[0.45, 0.45, 0.1]),
            "refund_suc_time": suc[rf_m].to_numpy(),
            "binlog_ts": (suc[rf_m] + pd.to_timedelta(rng.integers(1, 20, n_rp), unit="s")).to_numpy(),
        }
    )

    # ---------------- 用户注册时间(注册早于首单, T3) ----------------
    first_order = pd.Series(order_time.values).groupby(o_user).min()
    reg = pd.Series(pd.Timestamp("2024-06-01"), index=user_id) + pd.to_timedelta(
        rng.integers(0, 720 * 24 * 3600, n_users), unit="s")
    fo = first_order.reindex(user_id)
    back = pd.to_timedelta(np.round(rng.exponential(180, n_users) * 86400).astype("int64"), unit="s")
    sameday = rng.random(n_users) < 0.08
    reg_buyer = (fo - back).where(~pd.Series(sameday, index=user_id),
                                  fo - pd.to_timedelta(rng.integers(600, 10800, n_users), unit="s"))
    register_time = reg_buyer.fillna(reg).clip(lower=pd.Timestamp("2023-01-01"), upper=end)
    df_user = pd.DataFrame(
        {
            "user_id": user_id, "nick_name": nick, "gender": gender, "province": u_prov,
            "register_time": register_time.to_numpy(), "register_channel": reg_channel,
            "is_test_account": is_test_account,
            "binlog_ts": (register_time + pd.Timedelta(seconds=5)).to_numpy(),
        }
    )

    # ---------------- 写入 DuckDB ----------------
    con = duckdb.connect(str(DB_PATH))
    con.execute("create schema if not exists ods")
    tables = {
        "ods_user_info": df_user,
        "ods_order_info": df_order,
        "ods_order_item": df_item,
        "ods_payment_flow": df_payflow,
        "ods_exchange_rate": df_rate,
        "ods_refund_apply": df_refund_apply,
        "ods_refund_payment": df_refund_pay,
        "ods_logistics_order": df_lo,
        "ods_logistics_trace": df_trace,
    }
    for name, df in tables.items():
        con.register("v", df)
        con.execute(f"create or replace table ods.{name} as select * from v")
        con.unregister("v")
    con.execute("create or replace table ods.sim_meta as select ? as seed, ? as generated_at, ? as cfg",
                [cfg["seed"], datetime.now().isoformat(), json.dumps(cfg, ensure_ascii=False, default=str)])
    rows = {k: len(v) for k, v in tables.items()}
    con.close()
    print(json.dumps({"rows": rows, "orders": n_orders, "seconds": round(time.time() - t0, 1)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

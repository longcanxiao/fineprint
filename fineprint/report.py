#!/usr/bin/env python3
"""口径卡 → 自包含 HTML 报告(active 批次全量,单文件可直接分享)。

渲染纪律与发布状态机一致,不做自己的判断:
- publication_status 是唯一的门面状态(VERIFIED/TECHNICAL_ONLY/REVIEW_REQUIRED),
  confidence 只是叙述层互验置信度,降级为互验区的一个注脚;
- 公式按 formula authority 展示:machine → 组合器 top/defs/inline(发布权威),
  LLM 公式退居"解释与叙述";llm_fallback → 明确标注组合器未覆盖的机器原因;
- TECHNICAL_ONLY 的业务叙述折叠为"待审草稿",REVIEW_REQUIRED 不展示口径内容,
  只出问题摘要(评审也要溯源,证据表保留);
- 条款证据编号可点击跳转到卡内证据原文行(id/类型/模型/源文件/编译行号)。
无 publication_status/technical_facts 的旧批次卡按旧版视图诚实回退。
"""
import html
import json
from pathlib import Path

from fineprint.i18n import t
from fineprint.project import DbtProject
from fineprint.store import CaliberStore

CSS = """
:root { --ink:#1a222c; --sub:#46515e; --muted:#87909c; --line:#dfe3dc; --page:#f6f7f4;
  --surface:#fdfdfc; --accent:#2563c4; --accent-soft:rgba(37,99,196,.09);
  --warn:#b45309; --good:#1a7f37; --bad:#b23434; --code:#eef0ea; }
@media (prefers-color-scheme: dark) { :root { --ink:#e7ebf0; --sub:#aeb7c2; --muted:#737d89;
  --line:#2a3138; --page:#0e1114; --surface:#161a1f; --accent:#689fe8;
  --accent-soft:rgba(104,159,232,.13); --warn:#dd9a52; --good:#5cb374; --bad:#d97a72; --code:#1d2329; } }
* { box-sizing:border-box; } body { margin:0; background:var(--page); color:var(--ink);
  font:15px/1.7 "IBM Plex Sans","PingFang SC","Microsoft YaHei",sans-serif; }
.wrap { max-width:900px; margin:0 auto; padding:40px 24px 80px; }
h1 { font-size:26px; margin:0 0 6px; } .sub { color:var(--muted); font-size:13px; margin-bottom:28px; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:12px;
  padding:20px 24px; margin-bottom:20px; }
.card h2 { font-size:18px; margin:0 0 2px; display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }
.tgt { font:11.5px ui-monospace,monospace; color:var(--muted); }
.conf { font-size:11px; padding:1px 9px; border-radius:99px; border:1px solid; }
.c-high,.c-good { color:var(--good); } .c-medium,.c-warn { color:var(--warn); }
.c-low,.c-review,.c-bad { color:var(--bad); }
h3 { font-size:13px; margin:16px 0 6px; color:var(--sub); text-transform:uppercase;
  letter-spacing:.06em; font-family:ui-monospace,monospace; }
.def { font-size:15.5px; font-weight:600; margin:0 0 8px; }
ul { margin:0 0 8px; padding-left:1.3em; } li { margin-bottom:4px; }
a.ev { display:inline-block; margin-left:5px; padding:0 5px; font:10px ui-monospace,monospace;
  color:var(--accent); border:1px solid var(--accent); border-radius:5px; opacity:.85;
  text-decoration:none; }
pre { background:var(--code); border-radius:8px; padding:10px 14px; font:12px ui-monospace,monospace;
  overflow-x:auto; white-space:pre-wrap; word-break:break-all; margin:6px 0; }
.meta { color:var(--muted); font-size:12px; }
.gov { border-left:3px solid var(--warn); padding:6px 12px; background:rgba(180,83,9,.07);
  border-radius:0 8px 8px 0; font-size:13px; margin-top:8px; }
.gov code { font:11px ui-monospace,monospace; }
.caveat { color:var(--warn); font-size:13px; }
.fallback { border-left:3px solid var(--warn); padding:6px 12px; background:rgba(180,83,9,.07);
  border-radius:0 8px 8px 0; font-size:13px; margin:6px 0; }
.review-why { border-left:3px solid var(--bad); padding:6px 12px; background:rgba(178,52,52,.07);
  border-radius:0 8px 8px 0; font-size:13.5px; margin:10px 0; }
.draft { margin-top:6px; border:1px dashed var(--line); border-radius:8px; padding:4px 12px; }
.draft summary { cursor:pointer; color:var(--warn); font-size:13px; }
.draft .body { opacity:.75; padding-top:6px; }
.defrow { margin:6px 0; } .defrow .meta { margin-top:2px; }
.facts li code { font:11.5px ui-monospace,monospace; }
.llm-alt { color:var(--muted); font-size:12.5px; }
.llm-alt code { font:11px ui-monospace,monospace; }
.evd { margin-top:10px; font-size:12.5px; }
.evd summary { cursor:pointer; color:var(--accent); font:12px ui-monospace,monospace; }
.evd table { border-collapse:collapse; margin-top:8px; width:100%; }
.evd td { border-top:1px solid var(--line); padding:4px 8px 4px 0; vertical-align:top; color:var(--sub); }
.evd td code { font:11px ui-monospace,monospace; word-break:break-all; }
.evd tr:target td { background:var(--accent-soft); }
.evk { color:var(--accent); font:11px ui-monospace,monospace; white-space:nowrap; }
.evl { color:var(--muted); font:10.5px ui-monospace,monospace; }
"""

# 锚点跳转落在折叠的 <details> 里时先展开再滚动(部分浏览器不自动展开)
JS = """
function _openEv(){var el=location.hash&&document.getElementById(location.hash.slice(1));
  if(!el)return;var d=el.closest('details');if(d)d.open=true;el.scrollIntoView();}
addEventListener('hashchange',_openEv);addEventListener('DOMContentLoaded',_openEv);
"""

# 徽标/判定释义存 (zh, en) 双语对,渲染时经 t() 按当前语言取值
PUB_CHIP = {
    "VERIFIED": ("VERIFIED", "VERIFIED", "good"),
    "TECHNICAL_ONLY": ("TECHNICAL_ONLY · 叙述待审",
                       "TECHNICAL_ONLY · narrative pending review", "warn"),
    "REVIEW_REQUIRED": ("REVIEW_REQUIRED · 未过发布门禁",
                        "REVIEW_REQUIRED · failed the publishing gate", "bad"),
}
RACE_DESC = {"agree": ("结构一致", "structures agree"),
             "consistent": ("无矛盾", "no contradiction"),
             "prose": ("散文公式", "prose formula"),
             "disagree": ("存在分歧", "channels disagree"),
             "renderer_unsupported": ("组合器未覆盖", "not covered by the composer")}


def _race_desc(verdict) -> str:
    return t(*RACE_DESC.get(verdict, ("", "")))


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _fmt_clean(sql) -> str:
    return str(sql or "").replace('"', "")


def _keys(v) -> str:
    return t("、", ", ").join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v or "")


def _biz_body(biz: dict, badge) -> str:
    clauses = "".join(
        f'<li>{_esc(cl.get("text"))}' + "".join(badge(i) for i in cl.get("evidence_ids", []))
        + "</li>"
        for cl in biz.get("clauses", []))
    caveats = " ".join(_esc(x) for x in biz.get("caveats", []))
    return (f'<p class="def">{_esc(biz.get("definition"))}</p><ul>{clauses}</ul>'
            + (f'<p class="caveat">⚠ {caveats}</p>' if caveats else ""))


def _machine_formula(ff: dict) -> str:
    """组合器公式:top + 命名中间定义(各带定义处与粒度) + 完全内联展开。"""
    defs = ff.get("defs") or []
    top = ff.get("top") or ""
    parts = []
    def def_meta(d) -> str:
        return (t(f'定义于 {_esc(d.get("model"))} · 粒度 {_esc(_keys(d.get("grain")))}',
                  f'defined in {_esc(d.get("model"))} · grain {_esc(_keys(d.get("grain")))}')
                + (t(" · 聚合跨 join", " · aggregate across join") if d.get("join_context") else ""))

    if len(defs) == 1 and defs[0].get("name") == top:
        d = defs[0]
        parts.append(f'<pre>{_esc(_fmt_clean(d.get("expr")))}</pre>')
        parts.append(f'<p class="meta">{def_meta(d)}</p>')
    else:
        parts.append(f"<pre>{_esc(_fmt_clean(top))}</pre>")
        for d in defs:
            parts.append(
                f'<div class="defrow"><pre>{_esc(d.get("name"))} = {_esc(_fmt_clean(d.get("expr")))}</pre>'
                f'<p class="meta">{def_meta(d)}</p></div>')
    inline = ff.get("inline")
    shown = {str(d.get("expr")) for d in defs} | {str(top)}
    if inline and str(inline) not in shown:
        parts.append(f'<details class="evd"><summary>{t("完全内联展开", "fully inlined expansion")}</summary>'
                     f'<pre>{_esc(_fmt_clean(inline))}</pre></details>')
    return "".join(parts)


def _machine_facts(tf: dict, badge) -> str:
    """组合器证明的关键过滤/窗口/输出粒度——权威技术口径的正文,不再只活在证据表里。"""
    items = []
    kf = tf.get("key_filters") or {}
    for it in kf.get("items") or []:
        ev = badge(it["evidence"]) if it.get("evidence") else ""
        loc = f'{_esc(it.get("model"))}' + (f' L{it["line"]}' if it.get("line") else "")
        items.append(f'<li><code>{_esc(_fmt_clean(it.get("sql")))}</code>'
                     f' <span class="evl">{loc}</span>{ev}</li>')
    for w in (tf.get("window") or {}).get("items") or []:
        loc = f'{_esc(w.get("model"))}' + (f' L{w["line"]}' if w.get("line") else "")
        items.append(f'<li>{t("窗口", "window")}/{_esc(w.get("idiom"))}: '
                     f'<code>{_esc(_fmt_clean(w.get("sql")))}</code>'
                     f' <span class="evl">{loc}</span></li>')
    grain = tf.get("grain") or {}
    tail = (f'<p class="meta">{t("输出粒度", "output grain")} '
            f'{_esc(_keys(grain.get("keys")))} @ {_esc(grain.get("model"))}</p>'
            if grain.get("keys") else "")
    if not items and not tail:
        return ""
    return (f'<ul class="facts">{"".join(items)}</ul>{tail}')


def _evidence_details(c: dict, files: dict, badge_ids_only=False) -> str:
    evs = [e for e in c.get("evidence", []) if e.get("id")]
    if not evs:
        return ""
    key = _esc(c.get("metric_key"))
    rows = []
    for e in evs:
        model = e.get("model") or ""
        src = files.get(model, "")
        loc = _esc(model) + (f'<br><span class="evl">{_esc(src)}</span>' if src else "")
        line = (f'<span class="evl">{t("编译行", "compiled line")} L{e["line"]}</span>'
                if e.get("line") else "")
        rows.append(f'<tr id="ev-{key}-{_esc(e["id"])}"><td class="evk">{_esc(e["id"])}</td>'
                    f'<td>{_esc(e.get("kind"))}</td><td>{loc} {line}</td>'
                    f'<td><code>{_esc(e.get("text"))}</code></td></tr>')
    return ('<details class="evd"><summary>'
            + t(f"证据原文({len(evs)} 条,条款编号可点击跳转)",
                f"evidence texts ({len(evs)}; clause ids click through)")
            + f'</summary><table>{"".join(rows)}</table></details>')


def card_html(c: dict, files: dict | None = None) -> str:
    files = files or {}
    conf = c.get("confidence", "low")
    key = _esc(c.get("metric_key"))
    ev_by_id = {e.get("id"): e for e in c.get("evidence", []) if e.get("id")}

    def badge(i):
        e = ev_by_id.get(i)
        tip = f'[{e.get("kind")}] {e.get("model")} L{e.get("line")}: {str(e.get("text"))[:160]}' if e else ""
        return f'<a class="ev" href="#ev-{key}-{_esc(i)}" title="{_esc(tip)}">{_esc(i)}</a>'

    if c.get("status") == "review":
        return (f'<div class="card"><h2>{_esc(c["title"])} <span class="tgt">{_esc(c["target"])}</span>'
                f'<span class="conf c-review">{t("审核中", "under review")}</span></h2>'
                f'<p class="meta">'
                + t("双通道互验低置信,内容待人工审核后展示。",
                    "low dual-channel cross-validation confidence; content withheld until human review.")
                + "</p></div>")

    pub = c.get("publication_status")
    tf = c.get("technical_facts") or {}
    ff = tf.get("formula") or {}
    authority = ff.get("authority")
    biz, tech = c.get("business", {}), c.get("technical", {})
    v = c.get("validation", {})
    race = c.get("race") or {}

    chip = (f'<span class="conf c-{PUB_CHIP[pub][2]}">{t(*PUB_CHIP[pub][:2])}</span>' if pub in PUB_CHIP
            else f'<span class="conf c-{conf}">{conf}</span>')   # 旧批次卡回退置信徽标
    head = (f'<h2>{_esc(c["title"])} <span class="tgt">{_esc(c["target"])}</span>{chip}</h2>')
    meta_line = (f'<p class="meta">{t("生成于", "generated at")} {_esc(c.get("generated_at"))}'
                 f' · {_esc(c.get("llm_model"))}'
                 f' · run {_esc(c.get("run_id"))}'
                 f' · {t("图", "graph")} {_esc(str(c.get("graph_md5") or "")[:12])}</p>')

    # ── REVIEW_REQUIRED:不进正式正文,只出问题摘要(证据表保留供评审溯源)──
    if pub == "REVIEW_REQUIRED":
        why = []
        if race.get("verdict"):
            note = (race.get("detail") or {}).get("note") or ""
            why.append(t(f'赛马判定 {_esc(race["verdict"])}({_race_desc(race["verdict"])})',
                         f'race verdict {_esc(race["verdict"])} ({_race_desc(race["verdict"])})')
                       + (t(f":{_esc(note)}", f": {_esc(note)}") if note else ""))
        why += [t(f"组合器:{_esc(r)}", f"composer: {_esc(r)}") for r in ff.get("reasons") or []]
        if ff.get("rt_failed"):
            why.append(t("round-trip 校验失败(组合公式未能与通道一对账)",
                         "round-trip check failed (the composed formula did not reconcile "
                         "with channel one)"))
        if v.get("freetext_unverified"):
            why.append(t(f'叙述引用词表外 token:{_esc(v["freetext_unverified"])}',
                         f'narrative cites out-of-vocabulary tokens: {_esc(v["freetext_unverified"])}'))
        if v.get("unverified_clauses"):
            why.append(t(f'{v["unverified_clauses"]} 条业务条款未绑定证据',
                         f'{v["unverified_clauses"]} business clauses not bound to evidence'))
        rows = ("".join(f"<li>{w}</li>" for w in why)
                or f'<li>{t("见批次日志", "see the batch log")}</li>')
        return (f'<div class="card">{head}'
                f'<div class="review-why">'
                + t("未通过发布门禁,口径内容不在报告中展示;待人工评审。",
                    "failed the publishing gate; caliber content is withheld from the report, "
                    "pending human review.")
                + f'<ul>{rows}</ul></div>'
                f'{_evidence_details(c, files)}{meta_line}</div>')

    # ── 业务口径:VERIFIED 正式展示;TECHNICAL_ONLY 折叠为待审草稿 ──
    if pub == "TECHNICAL_ONLY":
        unv = v.get("freetext_unverified")
        why = t(f'——未过验字段 {_esc(json.dumps(unv, ensure_ascii=False))}' if unv else "",
                f' — unverified fields {_esc(json.dumps(unv, ensure_ascii=False))}' if unv else "")
        biz_html = ('<details class="draft"><summary>'
                    + t(f'业务叙述 · 待审草稿(叙述层未过互验{why},勿作口径依据)',
                        f'business narrative · draft pending review (the narrative failed '
                        f'cross-validation{why}; do not rely on it as caliber)')
                    + f'</summary><div class="body">{_biz_body(biz, badge)}</div></details>')
    else:
        biz_html = f'<h3>{t("业务口径", "business caliber")}</h3>{_biz_body(biz, badge)}'

    # ── 技术口径:按公式权威渲染 ──
    if authority == "machine":
        llm_f = tech.get("formula")
        llm_alt = ('<p class="llm-alt">'
                   + t("LLM 简化写法(解释与叙述,非发布口径):",
                       "LLM simplified form (explanation & narrative, not the published caliber): ")
                   + f'<code>{_esc(llm_f)}</code></p>' if llm_f else "")
        tech_html = (f'<h3>{t("技术口径 · 组合器(发布权威)", "technical caliber · composer (publishing authority)")}</h3>'
                     f'{_machine_formula(ff)}'
                     f'{_machine_facts(tf, badge)}{llm_alt}')
    elif authority == "llm_fallback":
        reasons = (t(";", "; ").join(str(r) for r in ff.get("reasons") or [])
                   or t("未给出机器原因", "no machine reason given"))
        tech_html = (f'<h3>{t("技术口径 · LLM 兜底", "technical caliber · LLM fallback")}</h3>'
                     f'<div class="fallback">'
                     + t(f'组合器未覆盖({_esc(reasons)}),当前公式为 LLM 生成,经双通道互验但非机器证明。',
                         f'not covered by the composer ({_esc(reasons)}); this formula is LLM-generated, '
                         f'cross-validated across both channels but not machine-proven.')
                     + '</div>'
                     f'<pre>{_esc(tech.get("formula"))}</pre>'
                     f'{_machine_facts(tf, badge)}')
    else:                                       # 旧批次卡:无权威字段,按旧视图回退
        tech_html = (f'<h3>{t("技术口径", "technical caliber")}</h3><pre>{_esc(tech.get("formula"))}</pre>'
                     + (f'<p class="meta">{t("时间窗", "time window")}: {_esc(tech.get("window"))}</p>'
                        if tech.get("window") else ""))

    # ── 互验区:置信度只是叙述层互验的注脚,不再冒充门面状态 ──
    s_same = not v.get("s_missing_by_llm") and not v.get("s_extra_by_llm")
    verify = t(f'源字段{"完全一致" if s_same else "存在差异"}'
               f' · 条件覆盖 {v.get("f1_covered", 0) * 100:.0f}%({v.get("f1_total", 0)} 条)'
               f' · 可疑 {len(v.get("f2_suspect", []))} · 未证条款 {v.get("unverified_clauses", 0)}',
               f'sources {"fully consistent" if s_same else "differ"}'
               f' · condition coverage {v.get("f1_covered", 0) * 100:.0f}% ({v.get("f1_total", 0)})'
               f' · suspect {len(v.get("f2_suspect", []))} · unproven clauses {v.get("unverified_clauses", 0)}')
    race_line = ""
    if race.get("verdict"):
        note = (race.get("detail") or {}).get("note") or ""
        race_line = (t(f' · 赛马 {_esc(race["verdict"])}({_race_desc(race["verdict"])})',
                       f' · race {_esc(race["verdict"])} ({_race_desc(race["verdict"])})')
                     + (t(f":{_esc(note)}", f": {_esc(note)}") if note else ""))
    verify = t(f"叙述互验置信度 {conf} · {verify}{race_line}",
               f"narrative cross-validation confidence {conf} · {verify}{race_line}")

    gov = c.get("governance", {}).get("duplicates", [])
    gov_html = ""
    if gov:
        rows = "<br>".join(f'<code>{_esc(p["a"])} ≍ {_esc(p["b"])}</code>' for p in gov)
        gov_html = ('<div class="gov">'
                    + t("治理提示 · 同源同构:", "governance hint · same source, same structure:")
                    + f'<br>{rows}</div>')

    return (f'<div class="card">{head}{biz_html}{tech_html}'
            f'<h3>{t("双通道互验", "dual-channel cross-validation")}</h3><p class="meta">{verify}</p>'
            f'{_evidence_details(c, files)}{gov_html}{meta_line}</div>')


def _model_files(project: DbtProject) -> dict:
    """展示模型名 → 源文件路径(证据行的文件锚点);图缺席/过旧时只降级掉文件列。"""
    try:
        from fineprint.tracing import load_graph
        g = load_graph(project.graph_path())
        out = {}
        for m in g["models"].values():
            out.setdefault(m["name"], m.get("src_path") or "")
        return out
    except Exception:
        return {}


def export_html(project: DbtProject, out: Path) -> int:
    store = CaliberStore(project.workspace / "store")
    d = store.active_dir()
    if d is None:
        raise FileNotFoundError(t("没有已发布的口径批次;请先执行 fineprint synth",
                                  "no published caliber batch; run fineprint synth first"))
    idx = store.index() or {}
    cards = []
    for f in sorted(d.glob("*.json")):
        if f.name != "index.json":
            cards.append(json.loads(f.read_text(encoding="utf-8")))
    files = _model_files(project)
    body = "".join(card_html(c, files) for c in cards)
    pubs: dict = {}
    for c in cards:
        k = c.get("publication_status") or (t("审核中", "under review") if c.get("status") == "review"
                                            else t("旧版卡", "legacy card"))
        pubs[k] = pubs.get(k, 0) + 1
    pub_line = " · ".join(f"{n} {k}" for k, n in sorted(pubs.items(), reverse=True))
    title = t("FinePrint 口径卡", "FinePrint Caliber Cards")
    sub = t(f'批次 {_esc(idx.get("run_id"))} · {_esc(idx.get("at"))} · {len(cards)} 个指标({pub_line})·\n'
            f'业务/技术双口径由血缘 × LLM 双通道互验生成,发布状态按状态机渲染,条款级证据编号可点击溯源',
            f'batch {_esc(idx.get("run_id"))} · {_esc(idx.get("at"))} · {len(cards)} metrics ({pub_line}) ·\n'
            f'business & technical calibers generated by lineage × LLM dual-channel cross-validation; '
            f'publication status rendered by the state machine; clause-level evidence ids trace to receipts')
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title><style>{CSS}</style><script>{JS}</script></head><body><div class="wrap">
<h1>{title}</h1>
<p class="sub">{sub}</p>
{body}</div></body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return len(cards)

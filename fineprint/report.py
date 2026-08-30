#!/usr/bin/env python3
"""口径卡 → 自包含 HTML 报告(active 批次全量,单文件可直接分享)。"""
import html
import json
from pathlib import Path

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
.c-high { color:var(--good); } .c-medium { color:var(--warn); } .c-low,.c-review { color:var(--bad); }
h3 { font-size:13px; margin:16px 0 6px; color:var(--sub); text-transform:uppercase;
  letter-spacing:.06em; font-family:ui-monospace,monospace; }
.def { font-size:15.5px; font-weight:600; margin:0 0 8px; }
ul { margin:0 0 8px; padding-left:1.3em; } li { margin-bottom:4px; }
.ev { display:inline-block; margin-left:5px; padding:0 5px; font:10px ui-monospace,monospace;
  color:var(--accent); border:1px solid var(--accent); border-radius:5px; opacity:.85; }
pre { background:var(--code); border-radius:8px; padding:10px 14px; font:12px ui-monospace,monospace;
  overflow-x:auto; white-space:pre-wrap; word-break:break-all; }
.meta { color:var(--muted); font-size:12px; }
.gov { border-left:3px solid var(--warn); padding:6px 12px; background:rgba(180,83,9,.07);
  border-radius:0 8px 8px 0; font-size:13px; margin-top:8px; }
.gov code { font:11px ui-monospace,monospace; }
.caveat { color:var(--warn); font-size:13px; }
"""


def _esc(s) -> str:
    return html.escape(str(s or ""))


def card_html(c: dict) -> str:
    conf = c.get("confidence", "low")
    if c.get("status") == "review":
        return (f'<div class="card"><h2>{_esc(c["title"])} <span class="tgt">{_esc(c["target"])}</span>'
                f'<span class="conf c-review">审核中</span></h2>'
                f'<p class="meta">双通道互验低置信,内容待人工审核后展示。</p></div>')
    biz, tech = c.get("business", {}), c.get("technical", {})
    v = c.get("validation", {})
    clauses = "".join(
        f'<li>{_esc(cl.get("text"))}' + "".join(f'<span class="ev">{_esc(i)}</span>' for i in cl.get("evidence_ids", []))
        + "</li>"
        for cl in biz.get("clauses", []))
    caveats = " ".join(_esc(x) for x in biz.get("caveats", []))
    gov = c.get("governance", {}).get("duplicates", [])
    gov_html = ""
    if gov:
        rows = "<br>".join(f'<code>{_esc(p["a"])} ≍ {_esc(p["b"])}</code>' for p in gov)
        gov_html = f'<div class="gov">治理提示 · 同源同构:<br>{rows}</div>'
    verify = (f'源字段{"完全一致" if not v.get("s_missing_by_llm") and not v.get("s_extra_by_llm") else "存在差异"}'
              f' · 条件覆盖 {v.get("f1_covered", 0) * 100:.0f}%({v.get("f1_total", 0)} 条)'
              f' · 可疑 {len(v.get("f2_suspect", []))} · 未证条款 {v.get("unverified_clauses", 0)}')
    return f"""<div class="card">
<h2>{_esc(c["title"])} <span class="tgt">{_esc(c["target"])}</span>
<span class="conf c-{conf}">{conf}</span></h2>
<h3>业务口径</h3>
<p class="def">{_esc(biz.get("definition"))}</p>
<ul>{clauses}</ul>
{f'<p class="caveat">⚠ {caveats}</p>' if caveats else ""}
<h3>技术口径</h3>
<pre>{_esc(tech.get("formula"))}</pre>
{f'<p class="meta">时间窗: {_esc(tech.get("window"))}</p>' if tech.get("window") else ""}
<h3>双通道互验</h3>
<p class="meta">{verify}</p>
{gov_html}
<p class="meta">生成于 {_esc(c.get("generated_at"))} · {_esc(c.get("llm_model"))} · run {_esc(c.get("run_id"))}</p>
</div>"""


def export_html(project: DbtProject, out: Path) -> int:
    store = CaliberStore(project.workspace / "store")
    d = store.active_dir()
    if d is None:
        raise FileNotFoundError("没有已发布的口径批次;请先执行 fineprint synth")
    idx = store.index() or {}
    cards = []
    for f in sorted(d.glob("*.json")):
        if f.name != "index.json":
            cards.append(json.loads(f.read_text()))
    body = "".join(card_html(c) for c in cards)
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>FinePrint 口径卡</title><style>{CSS}</style></head><body><div class="wrap">
<h1>FinePrint 口径卡</h1>
<p class="sub">批次 {_esc(idx.get("run_id"))} · {_esc(idx.get("at"))} · {len(cards)} 个指标 ·
业务/技术双口径由血缘 × LLM 双通道互验生成,条款级证据编号可溯源</p>
{body}</div></body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return len(cards)

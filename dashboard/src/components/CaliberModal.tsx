import { useEffect, useMemo, useState } from 'react'
import type { EChartsOption } from 'echarts'
import Chart from './Chart'
import { fetchDrift, fetchLineageGraph, type DriftEvent, type LineageGraph } from '../api'
import type { Tokens } from '../theme'

interface Clause { text: string; basis?: string; evidence_ids?: string[]; basis_verified?: boolean }
interface Evidence { id: string; kind: string; model?: string | null; line?: number | null; text: string }
interface GovPair { a: string; b: string; fingerprint: string }
interface TfDef {
  name: string; model?: string | null; column?: string; expr: string
  grain?: string[]; kind: string; join_context?: boolean
  branches?: { label: string; expr: string }[]
}
interface TfTarget { target?: string; status: string; top?: string | null; defs?: TfDef[]; reasons?: string[] }
interface TfFormula extends TfTarget { inline?: string | null; rt_failed?: boolean; evidence?: string[]; per_target?: TfTarget[] }
interface TechnicalFacts {
  formula: TfFormula
  key_filters?: { status: string; items?: unknown[]; ambiguous_items?: unknown[] }
  sources?: { status: string; items?: unknown[] }
  window?: { status: string; items?: unknown[]; unique_on?: Record<string, string[]> }
  grain?: { status: string; keys?: string[]; model?: string }
}
interface Race { verdict: string; detail?: Record<string, unknown> }
interface Caliber {
  metric_key: string; title: string; target?: string; query_filter?: string | null
  generated_at: string; llm_model?: string; confidence: string; status: string
  message?: string
  validation?: {
    s_missing_by_llm: string[]; s_extra_by_llm: string[]
    f1_total: number; f1_covered: number; f1_uncovered: string[]; quote_verify_fail: number
    unverified_clauses?: number
  }
  technical?: { formula: string; window?: string; special?: string[]; key_filters?: { text: string; layer: string }[]; summary?: string }
  business?: { definition: string; clauses?: Clause[]; caveats?: string[] }
  evidence?: Evidence[]
  governance?: { duplicates?: GovPair[] }
  technical_facts?: TechnicalFacts
  race?: Race
  publication_status?: string
  trace?: {
    depth: number; models_visited: string[]
    sources: { table: string; column: string }[]
    conditions: { kind: string; sql: string; src_path: string; line: number | null; row_level?: boolean; is_pure_key?: boolean }[]
  }
}

const CONF_LABEL: Record<string, [string, string]> = {
  high: ['高置信 · 双通道一致', 'conf-high'],
  medium: ['中置信 · 存在表述差异', 'conf-mid'],
  low: ['低置信 · 人工审核中', 'conf-low'],
}

const PUB_LABEL: Record<string, [string, string]> = {
  VERIFIED: ['VERIFIED · 机器无矛盾', 'conf-high'],
  TECHNICAL_ONLY: ['TECHNICAL_ONLY · 机器口径可用,叙述待审', 'conf-mid'],
  REVIEW_REQUIRED: ['REVIEW_REQUIRED · 须人工复核', 'conf-low'],
  BLOCKED: ['BLOCKED', 'conf-low'],
}

const RACE_LABEL: Record<string, string> = {
  agree: '组合器公式结构一致',
  consistent: '与组合器无机器矛盾(未达结构一致)',
  prose: 'LLM 公式非可解析 SQL,仅 token 级校验',
  disagree: '与组合器公式实锤矛盾',
  renderer_unsupported: '组合器未覆盖此构造',
}

const FACT_LABEL: Record<string, string> = {
  formula: '公式', key_filters: '过滤', sources: '源', window: '窗口', grain: '粒度',
}

const factCls = (s?: string) =>
  s === 'proven' ? 'conf-high' : s === 'unsupported' ? 'conf-low' : 'conf-mid'

const LAYER_X: Record<string, number> = { ods: 0, dwd: 1, dwm: 2, dm: 3, app: 4 }

const DRIFT_KIND: Record<string, string> = {
  source_added: '新增源字段', source_removed: '移除源字段',
  condition_added: '新增过滤条件', condition_removed: '移除过滤条件',
  semantic_added: '新增语义点', semantic_removed: '移除语义点',
  expr_changed: '表达式变更', expr_added: '新增链路列', expr_removed: '移除链路列',
}

export default function CaliberModal({ metricKey, title, tokens, onClose }: { metricKey: string; title: string; tokens: Tokens; onClose: () => void }) {
  const [card, setCard] = useState<Caliber | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [lg, setLg] = useState<LineageGraph | null>(null)
  const [driftEv, setDriftEv] = useState<DriftEvent[]>([])

  useEffect(() => {
    fetch(`/api/caliber/${metricKey}`)
      .then(r => { if (!r.ok) throw new Error(String(r.status)); return r.json() })
      .then(setCard)
      .catch(e => setErr(e.message === '404' ? '该指标的口径卡尚未生成' : `加载失败: ${e.message}`))
    fetchDrift(metricKey).then(r => setDriftEv(r.events)).catch(() => setDriftEv([]))
  }, [metricKey])

  const targetModel = useMemo(() => {
    const t = card?.target
    return t ? t.slice(0, t.lastIndexOf('.')) : null
  }, [card])

  useEffect(() => {
    const t = card?.target
    if (!t) return
    const i = t.lastIndexOf('.')
    fetchLineageGraph(t.slice(0, i), t.slice(i + 1)).then(setLg).catch(() => setLg(null))
  }, [card])

  const lineageOpt = useMemo<EChartsOption | null>(() => {
    if (!lg) return null
    const byLayer: Record<string, string[]> = {}
    lg.nodes.forEach(n => { (byLayer[n.layer] ??= []).push(n.id) })
    const pos: Record<string, [number, number]> = {}
    Object.entries(byLayer).forEach(([ly, ids]) => {
      ids.forEach((id, i) => { pos[id] = [(LAYER_X[ly] ?? 0) * 175, (i - (ids.length - 1) / 2) * 42] })
    })
    return {
      animation: false,
      tooltip: { show: true, confine: true },
      series: [{
        type: 'graph', layout: 'none', roam: false, silent: false,
        top: 24, bottom: 24, left: 84, right: 84,   // graph fit 只按节点中心,预留半个 symbol 宽/高
        data: lg.nodes.map(n => ({
          name: n.id, x: pos[n.id][0], y: pos[n.id][1],
          symbol: 'roundRect', symbolSize: [152, 24],
          itemStyle: n.id === targetModel
            ? { color: tokens.series[0], borderColor: tokens.series[0] }
            : { color: tokens.surface, borderColor: n.layer === 'ods' ? tokens.grid : tokens.baseline, borderWidth: 1 },
          label: {
            show: true, fontSize: 10, width: 140, overflow: 'truncate',
            color: n.id === targetModel ? '#ffffff' : tokens.ink,
          },
        })),
        links: lg.edges.map(e => ({ source: e.source, target: e.target })),
        lineStyle: { color: tokens.baseline, width: 1, curveness: 0.12 },
        edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 5,
      }],
    }
  }, [lg, targetModel, tokens])

  const canvasH = useMemo(() => {
    if (!lg) return 0
    const byLayer: Record<string, number> = {}
    lg.nodes.forEach(n => { byLayer[n.layer] = (byLayer[n.layer] ?? 0) + 1 })
    return Math.min(300, Math.max(150, Math.max(...Object.values(byLayer)) * 42 + 40))
  }, [lg])

  useEffect(() => {
    const fn = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', fn)
    return () => window.removeEventListener('keydown', fn)
  }, [onClose])

  const [confLabel, confCls] = card ? (CONF_LABEL[card.confidence] ?? ['未知', 'conf-low']) : ['', '']
  const evd = card?.evidence ?? []
  const govDups = card?.governance?.duplicates ?? []

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="m-title">{title} {card?.target && <span className="m-target">{card.target}</span>}</div>
            {card && <span className={`conf ${confCls}`}>{confLabel}</span>}
            {card?.publication_status && PUB_LABEL[card.publication_status] && (
              <span className={`conf ${PUB_LABEL[card.publication_status][1]}`} style={{ marginLeft: 6 }}>
                {PUB_LABEL[card.publication_status][0]}
              </span>
            )}
          </div>
          <button className="m-close" onClick={onClose} aria-label="关闭">×</button>
        </div>
        {!card && !err && <div className="loading">口径卡加载中…</div>}
        {err && <div className="loading">{err}</div>}
        {card && card.status === 'review' && (
          <div className="review-note">
            {card.message ?? '双通道互验低置信,该口径已进入人工审核队列,内容暂不展示。'}
          </div>
        )}
        {card && card.technical && card.business && (
          <div className="modal-body">
            <section>
              <h3>业务口径</h3>
              <p className="biz-def">{card.business.definition}</p>
              <ul className="clauses">
                {(card.business.clauses ?? []).map((c, i) => (
                  <li key={i} title={c.basis ? `证据原文: ${c.basis}` : undefined}>
                    {c.text}
                    {(c.evidence_ids ?? []).map(id => <span className="ev-tag" key={id}>{id}</span>)}
                    {c.basis_verified === false && <span className="ev-tag ev-warn" title="该条款未能绑定确定性证据">未证</span>}
                  </li>
                ))}
              </ul>
              {(card.business.caveats ?? []).length > 0 && (
                <div className="caveat">⚠ {(card.business.caveats ?? []).join(' ')}</div>
              )}
            </section>
            <section>
              <h3>技术口径 <span className="m-target">LLM 归并 · 发布权威(赛马期)</span></h3>
              <pre className="formula">{card.technical.formula}</pre>
              {card.technical.window && <p className="tech-win">时间窗/统计日: {card.technical.window}</p>}
              {(card.technical.special ?? []).length > 0 && (
                <div className="chips">{(card.technical.special ?? []).map((s, i) => <span className="chip" key={i}>{s}</span>)}</div>
              )}
              {card.query_filter && <p className="tech-win">取数过滤: {card.query_filter}</p>}
            </section>
            {card.technical_facts && (
              <section>
                <h3>机器口径 <span className="m-target">确定性组合器合成 · 与上方 LLM 口径双写赛马</span></h3>
                <div className="chips">
                  {Object.entries(FACT_LABEL).map(([k, lb]) => {
                    const st = (card.technical_facts as unknown as Record<string, { status?: string } | undefined>)[k]?.status
                    return st ? <span key={k} className={`conf ${factCls(st)}`}>{lb} {st}</span> : null
                  })}
                  {card.race && (
                    <span className={`conf ${card.race.verdict === 'disagree' ? 'conf-low' : card.race.verdict === 'agree' ? 'conf-high' : 'conf-mid'}`}>
                      赛马 {card.race.verdict}
                    </span>
                  )}
                </div>
                {(card.technical_facts.formula.per_target ?? [{ ...card.technical_facts.formula, target: undefined }]).map((t, ti) => (
                  <div key={ti}>
                    {t.target && <p className="tech-win">目标 {t.target}</p>}
                    {t.top && <pre className="formula">{t.top}</pre>}
                    {(t.defs ?? []).length > 0 && (
                      <ul className="refs">
                        {(t.defs ?? []).map((d, i) => (
                          <li key={i}>
                            <span>
                              <span className="ev-tag">{d.kind}</span>
                              <code>{d.name} := {d.branches ? `UNION ${d.branches.length} 分支(值=行所属分支的表达式)` : d.expr.replace(/"/g, '')}</code>
                              {(d.grain ?? []).length > 0 && <span className="ref-loc"> · per {(d.grain ?? []).join(', ')}</span>}
                              {d.branches && (
                                <div className="gov-reason">
                                  {d.branches.slice(0, 4).map((b, bi) => <div key={bi}><code>{b.label}: {b.expr.replace(/"/g, '')}</code></div>)}
                                  {d.branches.length > 4 && <div>… 另 {d.branches.length - 4} 个分支</div>}
                                </div>
                              )}
                            </span>
                            <span className="ref-loc">{d.model ?? ''}{d.join_context ? ' · join 上下文' : ''}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
                {(card.technical_facts.formula.reasons ?? []).length > 0 && (
                  <p className="src-list">{(card.technical_facts.formula.reasons ?? []).map((r, i) => <span key={i}>◦ {r}<br /></span>)}</p>
                )}
                {(card.technical_facts.grain?.keys ?? []).length > 0 && (
                  <p className="tech-win">输出粒度: {(card.technical_facts.grain?.keys ?? []).join(', ')}(定义于 {card.technical_facts.grain?.model})</p>
                )}
              </section>
            )}
            {govDups.length > 0 && (
              <section>
                <h3>治理提示</h3>
                <ul className="refs">
                  {govDups.map((p, i) => (
                    <li key={i}>
                      <code>{p.a} ≍ {p.b}</code>
                      <span className="ref-loc">同源同构 · 指纹 {p.fingerprint.slice(0, 8)}</span>
                    </li>
                  ))}
                </ul>
                <p className="src-list">以上列对由指纹扫描自动发现:同一 ODS 源与等价条件在多处物化,建议收敛到单一口径出口。</p>
              </section>
            )}
            {card.validation && (
              <section>
                <h3>双通道互验</h3>
                <p className="verify">
                  血缘通道 vs LLM 通道:源字段{card.validation.s_missing_by_llm.length + card.validation.s_extra_by_llm.length === 0 ? '完全一致' : `差异 ${card.validation.s_missing_by_llm.length} 漏 / ${card.validation.s_extra_by_llm.length} 多`}
                  ;关键过滤覆盖 {(card.validation.f1_covered * 100).toFixed(0)}%({card.validation.f1_total} 条)
                  {card.validation.quote_verify_fail > 0 && `;${card.validation.quote_verify_fail} 条引用未过原文校验`}
                  {(card.validation.unverified_clauses ?? 0) > 0 && `;${card.validation.unverified_clauses} 条业务条款未绑定证据`}
                </p>
                {card.race && (
                  <p className="verify">
                    公式赛马:<span className={`ev-tag ${card.race.verdict === 'disagree' ? 'ev-warn' : ''}`}>{card.race.verdict}</span>
                    {' '}{RACE_LABEL[card.race.verdict] ?? ''}
                    {card.race.verdict === 'disagree' && card.race.detail != null && (
                      <code className="gov-code">{JSON.stringify(card.race.detail).replace(/"/g, '').slice(0, 160)}</code>
                    )}
                  </p>
                )}
              </section>
            )}
            {lg && lineageOpt && (
              <section>
                <h3>血缘画布 <span className="m-target">ODS → DWD → DWM → DM → APP · {lg.nodes.length} 节点 {lg.edges.length} 边 · 蓝色为目标模型</span></h3>
                <div className="lineage-canvas"><Chart option={lineageOpt} height={canvasH} /></div>
              </section>
            )}
            {driftEv.length > 0 && (
              <section>
                <h3>口径变更历史 <span className="m-target">{driftEv.length} 条漂移事件 · 快照对比自动检测</span></h3>
                <ul className="refs">
                  {driftEv.slice(0, 8).map((e, i) => (
                    <li key={i}>
                      <span>
                        <span className={`tier ${e.severity === 'high' ? 'tier-a' : 'tier-b'}`}>{e.severity}</span>
                        {DRIFT_KIND[e.kind] ?? e.kind}
                        {(e.detail.sql ?? e.detail.source ?? e.detail.column) && (
                          <code className="gov-code">{e.detail.sql ?? e.detail.source ?? e.detail.column}</code>
                        )}
                      </span>
                      <span className="ref-loc">{e.detected_at.replace('T', ' ')}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {card.trace && (
              <section>
                <h3>证据清单 <span className="m-target">{card.trace.depth} 层链路 · {card.trace.models_visited.length} 个模型 · {card.trace.sources.length} 个 ODS 源字段</span></h3>
                <ul className="refs">
                  {(evd.length > 0
                    ? evd.filter(e => !e.id.startsWith('X')).slice(0, 12).map((e, i) => (
                      <li key={i}>
                        <span className="ev-tag">{e.id}</span>
                        <code>{e.text.replace(/"/g, '')}</code>
                        <span className="ref-loc">{e.model ?? ''}{e.line ? `:L${e.line}` : ''}</span>
                      </li>
                    ))
                    : card.trace.conditions.filter(c => !c.is_pure_key).slice(0, 10).map((c, i) => (
                      <li key={i}>
                        <code>[{c.kind}] {c.sql.replace(/"/g, '')}</code>
                        <span className="ref-loc">{c.src_path.split('/').pop()}{c.line ? `:L${c.line}` : ''}</span>
                      </li>
                    )))}
                </ul>
                <p className="src-list">源字段: {card.trace.sources.map(s => `${s.table}.${s.column}`).join(' · ')}</p>
              </section>
            )}
            <div className="m-foot">生成于 {card.generated_at.replace('T', ' ')} · {card.llm_model} · 血缘引擎 M3 + 口径合成 M4</div>
          </div>
        )}
      </div>
    </div>
  )
}

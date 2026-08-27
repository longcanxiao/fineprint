import { useEffect, useState } from 'react'
import { fetchDrift, fetchGovReport, type DriftEvent, type GovReport } from '../api'

const KIND_LABEL: Record<string, string> = {
  source_added: '新增源字段', source_removed: '移除源字段',
  condition_added: '新增过滤条件', condition_removed: '移除过滤条件',
  semantic_added: '新增语义点', semantic_removed: '移除语义点',
  expr_changed: '表达式变更', expr_added: '新增链路列', expr_removed: '移除链路列',
  metric_added: '指标上线', metric_removed: '指标下线',
  target_changed: '目标列改指向', query_filter_changed: '取数过滤变更',
}

const evBrief = (e: DriftEvent) =>
  e.detail.sql ?? e.detail.source ?? e.detail.column ?? ''

export default function GovernancePanel({ onClose }: { onClose: () => void }) {
  const [report, setReport] = useState<GovReport | null>(null)
  const [drift, setDrift] = useState<DriftEvent[] | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([fetchGovReport().then(setReport), fetchDrift().then(r => setDrift(r.events))])
      .catch(e => setErr(String(e.message ?? e)))
  }, [])

  useEffect(() => {
    const fn = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', fn)
    return () => window.removeEventListener('keydown', fn)
  }, [onClose])

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="m-title">指标治理台</div>
            {report?.generated_at && (
              <span className="m-target">
                指纹扫描 A 档 {report.a_tier_pairs} 对直判 + B 档 {report.b_tier_pairs} 对 LLM 仲裁{(report.b_tier_skipped ?? 0) > 0 && ` · ${report.b_tier_skipped} 对超上限未仲裁`} · {report.generated_at.replace('T', ' ')}
              </span>
            )}
          </div>
          <button className="m-close" onClick={onClose} aria-label="关闭">×</button>
        </div>
        {!report && !err && <div className="loading">治理数据加载中…</div>}
        {err && <div className="loading">加载失败: {err}</div>}
        {report && (
          <div className="modal-body">
            <section>
              <h3>重复建设 <span className="m-target">{report.duplicates.length} 对 · 同源同条件同语义,建议收敛</span></h3>
              <ul className="refs">
                {report.duplicates.map((p, i) => (
                  <li key={i} title={p.suggestion}>
                    <span>
                      <span className={`tier tier-${p.tier.toLowerCase()}`}>{p.tier} 档</span>
                      <code>{p.a} ≡ {p.b}</code>
                      {p.tier === 'B' && p.reason && <div className="gov-reason">{p.reason}</div>}
                    </span>
                    <span className="ref-loc">指纹 {p.fingerprint.slice(0, 8)}</span>
                  </li>
                ))}
              </ul>
            </section>
            <section>
              <h3>同源不同义 <span className="m-target">{report.distinct.length} 对 · A 档聚合直判 / B 档 LLM 仲裁,不收敛</span></h3>
              {(report.b_tier_skipped ?? 0) > 0 && (
                <p className="src-list">⚠ 另有 {report.b_tier_skipped} 对 B 档候选超出 max_llm_pairs 上限未仲裁,本清单不完整。</p>
              )}
              <ul className="refs">
                {report.distinct.map((p, i) => (
                  <li key={i}>
                    <span>
                      <span className="tier tier-ok">{p.tier === 'A' ? 'A 直判' : 'B·LLM'}</span>
                      <code>{p.a} ~ {p.b}</code>
                      {p.reason && <div className="gov-reason">{p.reason}</div>}
                    </span>
                    <span className="ref-loc">指纹 {p.fingerprint.slice(0, 8)}</span>
                  </li>
                ))}
              </ul>
            </section>
            {(report.families ?? []).length > 0 && (
              <section>
                <h3>同指标家族·不同粒度 <span className="m-target">{report.families!.length} 对 · 非重复,建议统一命名口径</span></h3>
                <ul className="refs">
                  {report.families!.map((p, i) => (
                    <li key={i}>
                      <span>
                        <span className="tier tier-b">家族</span>
                        <code>{p.a} ~ {p.b}</code>
                        <div className="gov-reason">粒度 [{p.grain_a.join(', ') || '明细'}] vs [{p.grain_b.join(', ') || '明细'}]</div>
                      </span>
                      <span className="ref-loc">指纹 {p.fingerprint.slice(0, 8)}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <section>
              <h3>口径漂移事件 <span className="m-target">{drift?.length ?? 0} 条 · 每次重建后快照对比自动检测</span></h3>
              {(drift ?? []).length === 0
                ? <p className="src-list">暂无漂移事件:自基线快照以来所有指标口径保持稳定。</p>
                : (
                  <ul className="refs">
                    {(drift ?? []).slice(0, 30).map((e, i) => (
                      <li key={i}>
                        <span>
                          <span className={`tier ${e.severity === 'high' ? 'tier-a' : 'tier-b'}`}>{e.severity}</span>
                          <strong className="gov-metric">{e.metric_key}</strong> {KIND_LABEL[e.kind] ?? e.kind}
                          {evBrief(e) && <div className="gov-reason"><code>{evBrief(e)}</code></div>}
                        </span>
                        <span className="ref-loc">{e.detected_at.replace('T', ' ')}</span>
                      </li>
                    ))}
                  </ul>
                )}
            </section>
            <div className="m-foot">指纹 = ODS 源字段集 + 归一化业务条件集 · 血缘直系已豁免 · 治理引擎 M5</div>
          </div>
        )}
      </div>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { fetchDrift, fetchGovReport, type DriftEvent, type GovReport } from '../api'
import { t } from '../i18n'

const KIND_LABEL: Record<string, string> = {
  source_added: t('新增源字段', 'source column added'), source_removed: t('移除源字段', 'source column removed'),
  condition_added: t('新增过滤条件', 'filter condition added'), condition_removed: t('移除过滤条件', 'filter condition removed'),
  semantic_added: t('新增语义点', 'semantic point added'), semantic_removed: t('移除语义点', 'semantic point removed'),
  expr_changed: t('表达式变更', 'expression changed'), expr_added: t('新增链路列', 'lineage column added'), expr_removed: t('移除链路列', 'lineage column removed'),
  metric_added: t('指标上线', 'metric added'), metric_removed: t('指标下线', 'metric removed'),
  target_changed: t('目标列改指向', 'target column repointed'), query_filter_changed: t('取数过滤变更', 'query filter changed'),
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
            <div className="m-title">{t('指标治理台', 'Metric Governance')}</div>
            {report?.generated_at && (
              <span className="m-target">
                {t('指纹扫描 A 档 ', 'fingerprint scan: ')}{report.a_tier_pairs}{t(' 对直判 + B 档 ', ' tier-A pairs auto-judged + ')}{report.b_tier_pairs}{t(' 对 LLM 仲裁', ' tier-B pairs LLM-arbitrated')}{(report.b_tier_skipped ?? 0) > 0 && t(` · ${report.b_tier_skipped} 对超上限未仲裁`, ` · ${report.b_tier_skipped} pairs over cap, not arbitrated`)} · {report.generated_at.replace('T', ' ')}
              </span>
            )}
          </div>
          <button className="m-close" onClick={onClose} aria-label={t('关闭', 'Close')}>×</button>
        </div>
        {!report && !err && <div className="loading">{t('治理数据加载中…', 'Loading governance data…')}</div>}
        {err && <div className="loading">{t('加载失败: ', 'Failed to load: ')}{err}</div>}
        {report && (
          <div className="modal-body">
            <section>
              <h3>{t('重复建设', 'Duplicate builds')} <span className="m-target">{report.duplicates.length}{t(' 对 · 同源同条件同语义,建议收敛', ' pairs · same sources, same conditions, same semantics — consider converging')}</span></h3>
              <ul className="refs">
                {report.duplicates.map((p, i) => (
                  <li key={i} title={p.suggestion}>
                    <span>
                      <span className={`tier tier-${p.tier.toLowerCase()}`}>{t(`${p.tier} 档`, `tier ${p.tier}`)}</span>
                      <code>{p.a} ≡ {p.b}</code>
                      {(p.exposures_a || p.exposures_b) && (
                        <span className="ev-tag" title={t(`a 喂 ${(p.exposures_a ?? []).join('、') || '无'};b 喂 ${(p.exposures_b ?? []).join('、') || '无'}`, `a feeds ${(p.exposures_a ?? []).join(', ') || 'none'}; b feeds ${(p.exposures_b ?? []).join(', ') || 'none'}`)}>
                          {t('看板 ', 'dashboards ')}{(p.exposures_a ?? []).length} vs {(p.exposures_b ?? []).length}
                        </span>
                      )}
                      {p.tier === 'B' && p.reason && <div className="gov-reason">{p.reason}</div>}
                    </span>
                    <span className="ref-loc">{t('指纹 ', 'fingerprint ')}{p.fingerprint.slice(0, 8)}</span>
                  </li>
                ))}
              </ul>
            </section>
            <section>
              <h3>{t('同源不同义', 'Same sources, different semantics')} <span className="m-target">{report.distinct.length}{t(' 对 · A 档聚合直判 / B 档 LLM 仲裁,不收敛', ' pairs · tier A judged by aggregation shape / tier B by LLM — kept distinct')}</span></h3>
              {(report.b_tier_skipped ?? 0) > 0 && (
                <p className="src-list">{t(`⚠ 另有 ${report.b_tier_skipped} 对 B 档候选超出 max_llm_pairs 上限未仲裁,本清单不完整。`, `⚠ ${report.b_tier_skipped} more tier-B candidates exceeded the max_llm_pairs cap and were not arbitrated; this list is incomplete.`)}</p>
              )}
              <ul className="refs">
                {report.distinct.map((p, i) => (
                  <li key={i}>
                    <span>
                      <span className="tier tier-ok">{p.tier === 'A' ? t('A 直判', 'A auto') : 'B·LLM'}</span>
                      <code>{p.a} ~ {p.b}</code>
                      {p.reason && <div className="gov-reason">{p.reason}</div>}
                    </span>
                    <span className="ref-loc">{t('指纹 ', 'fingerprint ')}{p.fingerprint.slice(0, 8)}</span>
                  </li>
                ))}
              </ul>
            </section>
            {(report.families ?? []).length > 0 && (
              <section>
                <h3>{t('同指标家族·不同粒度', 'Same metric family, different grain')} <span className="m-target">{report.families!.length}{t(' 对 · 非重复,建议统一命名口径', ' pairs · not duplicates — consider a consistent naming convention')}</span></h3>
                <ul className="refs">
                  {report.families!.map((p, i) => (
                    <li key={i}>
                      <span>
                        <span className="tier tier-b">{t('家族', 'family')}</span>
                        <code>{p.a} ~ {p.b}</code>
                        <div className="gov-reason">{t('粒度', 'grain')} [{p.grain_a.join(', ') || t('明细', 'row-level')}] vs [{p.grain_b.join(', ') || t('明细', 'row-level')}]</div>
                      </span>
                      <span className="ref-loc">{t('指纹 ', 'fingerprint ')}{p.fingerprint.slice(0, 8)}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {(report.row_mismatch ?? []).length > 0 && (
              <section>
                <h3>{t('疑似重复 · 基数未证', 'Suspected duplicates · row basis unproven')} <span className="m-target">{report.row_mismatch!.length}{t(' 对 · 值来源相同但途经 join 拓扑不同,须人工确认行基数', ' pairs · same value sources but different join topology — row basis needs human confirmation')}</span></h3>
                <ul className="refs">
                  {report.row_mismatch!.map((p, i) => (
                    <li key={i}>
                      <span>
                        <span className="tier tier-b">{t('基数未证', 'basis unproven')}</span>
                        <code>{p.a} ≈ {p.b}</code>
                        {p.same_base && <span className="ev-tag ev-warn" title={t('列基名一致,疑似度更高', 'identical column base names — higher suspicion')}>{t('同基名', 'same base')}</span>}
                        {(p.exposures_a || p.exposures_b) && (
                          <span className="ev-tag" title={t(`a 喂 ${(p.exposures_a ?? []).join('、') || '无'};b 喂 ${(p.exposures_b ?? []).join('、') || '无'}`, `a feeds ${(p.exposures_a ?? []).join(', ') || 'none'}; b feeds ${(p.exposures_b ?? []).join(', ') || 'none'}`)}>
                            {t('看板 ', 'dashboards ')}{(p.exposures_a ?? []).length} vs {(p.exposures_b ?? []).length}
                          </span>
                        )}
                        <div className="gov-reason">
                          {t('行集差异:仅 a 途经 [', 'Row-set difference: only a passes through [')}{p.rowset_only_a.join(', ') || '—'}{t('] · 仅 b 途经 [', '] · only b passes through [')}{p.rowset_only_b.join(', ') || '—'}]
                          {t('——一对多 join 可放大聚合值,join 键唯一性 SQL 未自证,既不判重复也不判不同义', ' — a one-to-many join can inflate aggregates; join-key uniqueness is not self-proven in the SQL, so the pair is judged neither duplicate nor distinct')}
                        </div>
                      </span>
                      <span className="ref-loc">{t('指纹 ', 'fingerprint ')}{p.fingerprint.slice(0, 8)}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {(report.sql_quality ?? []).length > 0 && (
              <section>
                <h3>{t('SQL 质量立项', 'SQL quality findings')} <span className="m-target">{report.sql_quality!.length}{t(' 项 · 行数聚合跨 join,计数对象未自证,须人工明确', ' items · row-count aggregation spans a join; the counted entity is not self-evident — needs human clarification')}</span></h3>
                <ul className="refs">
                  {report.sql_quality!.map((q, i) => (
                    <li key={i} title={q.suggestion}>
                      <span>
                        <span className="tier tier-a">{t('质量', 'quality')}</span>
                        <code>{q.model}.{q.column}</code>
                        <div className="gov-reason">{t('行集 ', 'row set ')}{q.tables.join(' ⋈ ')} · {q.reason}</div>
                      </span>
                      <span className="ref-loc">{q.line ? `L${q.line}` : ''}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <section>
              <h3>{t('口径漂移事件', 'Definition drift events')} <span className="m-target">{drift?.length ?? 0}{t(' 条 · 每次重建后快照对比自动检测', ' events · auto-detected by snapshot diff after each rebuild')}</span></h3>
              {(drift ?? []).length === 0
                ? <p className="src-list">{t('暂无漂移事件:自基线快照以来所有指标口径保持稳定。', 'No drift events: all metric definitions have been stable since the baseline snapshot.')}</p>
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
            <div className="m-foot">{t('指纹 = ODS 源字段集 + 归一化业务条件集 · 血缘直系已豁免 · 治理引擎 M5', 'fingerprint = ODS source-column set + normalized business-condition set · direct lineage exempted · governance engine M5')}</div>
          </div>
        )}
      </div>
    </div>
  )
}

import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import Chart from './Chart'
import type { Card } from '../api'
import type { Tokens } from '../theme'
import { t } from '../i18n'

export interface MetricDef {
  key: string
  label: string
  fmt: (v: number | null | undefined) => string
  unit?: string
  goodDir: 1 | -1          // 上升是好事(1)还是坏事(-1)
  caliber: string          // 口径占位(M4 由智能系统自动生成)
}

export default function MetricCard({ def, card, tokens, onCaliber, driftCount = 0 }: { def: MetricDef; card?: Card; tokens: Tokens; onCaliber: (key: string, title: string) => void; driftCount?: number }) {
  const v = card?.value ?? null
  const prev = card?.prev ?? null
  const delta = v != null && prev != null && prev !== 0 ? (v - prev) / Math.abs(prev) : null

  const cls = delta == null || Math.abs(delta) < 0.0005
    ? 'flat'
    : delta > 0
      ? (def.goodDir === 1 ? 'up-good' : 'up-bad')
      : (def.goodDir === 1 ? 'down-bad' : 'down-good')
  const arrow = delta == null || Math.abs(delta) < 0.0005 ? '' : delta > 0 ? '↑' : '↓'

  const spark = useMemo<EChartsOption>(() => ({
    animation: false,
    grid: { left: 0, right: 0, top: 4, bottom: 0 },
    xAxis: { type: 'category', show: false, data: (card?.spark ?? []).map(p => p.dt) },
    yAxis: { type: 'value', show: false, scale: true },
    tooltip: { show: false },
    series: [{
      type: 'line', silent: true, showSymbol: false, symbol: 'none',
      lineStyle: { width: 1.5, color: tokens.series[0] },
      data: (card?.spark ?? []).map(p => p.v),
    }],
  }), [card, tokens])

  return (
    <div className="card">
      <div className="k">
        <span>
          {def.label}
          {driftCount > 0 && (
            <button className="drift-badge" onClick={() => onCaliber(def.key, def.label)}
              title={t(`该指标口径近期发生 ${driftCount} 处变更,点击查看变更历史`, `This metric's definition changed ${driftCount} time(s) recently — click to see the history`)}>{t('口径变更', 'drift')}</button>
          )}
        </span>
        <button className="cal" onClick={() => onCaliber(def.key, def.label)} title={t('查看业务口径与技术口径(血缘+LLM 双通道合成)', 'View business & technical definition (lineage + LLM dual-channel synthesis)')}>{t('口径 ⓘ', 'definition ⓘ')}</button>
      </div>
      <div className="v">{def.fmt(v)}{def.unit ? <span className="unit">{def.unit}</span> : null}</div>
      <div className={`d ${cls}`}>
        <span className="lbl">{t('环比 ', 'vs prev ')}</span>
        {delta == null ? '–' : `${arrow} ${(Math.abs(delta) * 100).toFixed(1)}%`}
      </div>
      <Chart option={spark} height={40} />
    </div>
  )
}

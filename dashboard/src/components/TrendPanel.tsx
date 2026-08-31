import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import Chart from './Chart'
import type { Trend } from '../api'
import type { Tokens } from '../theme'
import { t } from '../i18n'
import { fmtAxisMoney, fmtTipMoney } from '../format'

const axisCommon = (t: Tokens) => ({
  axisLine: { lineStyle: { color: t.baseline } },
  axisTick: { show: false },
  axisLabel: { color: t.muted, fontSize: 11 },
})

export default function TrendPanel({ trend, tokens }: { trend: Trend | null; tokens: Tokens }) {
  const dts = useMemo(() => (trend?.daily ?? []).map(r => r.dt.slice(5)), [trend])

  const gmvOpt = useMemo<EChartsOption>(() => ({
    animationDuration: 300,
    grid: { left: 56, right: 16, top: 26, bottom: 24 },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: tokens.baseline } },
      backgroundColor: tokens.surface, borderColor: tokens.grid, textStyle: { color: tokens.ink, fontSize: 12 },
      valueFormatter: (v) => fmtTipMoney(v as number),
    },
    xAxis: { type: 'category', data: dts, ...axisCommon(tokens) },
    yAxis: {
      type: 'value', splitLine: { lineStyle: { color: tokens.grid } }, axisLabel: { color: tokens.muted, fontSize: 11, formatter: fmtAxisMoney },
    },
    series: [{
      name: 'GMV', type: 'bar', barWidth: '58%',
      itemStyle: { color: tokens.series[0], borderRadius: [3, 3, 0, 0] },
      data: (trend?.daily ?? []).map(r => Math.round(r.gmv)),
    }],
  }), [trend, dts, tokens])

  const rrOpt = useMemo<EChartsOption>(() => ({
    animationDuration: 300,
    grid: { left: 56, right: 16, top: 26, bottom: 24 },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: tokens.baseline } },
      backgroundColor: tokens.surface, borderColor: tokens.grid, textStyle: { color: tokens.ink, fontSize: 12 },
      valueFormatter: (v) => v == null ? '–' : ((v as number) * 100).toFixed(2) + '%',
    },
    xAxis: { type: 'category', data: dts, ...axisCommon(tokens) },
    yAxis: {
      type: 'value', scale: true,
      splitLine: { lineStyle: { color: tokens.grid } },
      axisLabel: { color: tokens.muted, fontSize: 11, formatter: (v: number) => (v * 100).toFixed(1) + '%' },
    },
    series: [{
      name: t('近14天退款率', '14-day refund rate'), type: 'line', showSymbol: false, symbolSize: 8,
      lineStyle: { width: 2, color: tokens.series[1] }, itemStyle: { color: tokens.series[1] },
      data: (trend?.daily ?? []).map(r => r.refund_rate_14d),
    }],
  }), [trend, dts, tokens])

  return (
    <div className="panel">
      <h2>{t('GMV 与近14天退款率', 'GMV & 14-day refund rate')} <span className="note">{t('同轴联动 · 悬浮查看逐日数值', 'shared axis · hover for daily values')}</span></h2>
      <Chart option={gmvOpt} height={210} group="trend" />
      <Chart option={rrOpt} height={150} group="trend" />
    </div>
  )
}

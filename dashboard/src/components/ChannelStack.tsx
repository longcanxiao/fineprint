import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import Chart from './Chart'
import type { Trend } from '../api'
import { CHANNELS, CHANNEL_LABEL, type Tokens } from '../theme'
import { t } from '../i18n'
import { fmtAxisMoney, fmtTipMoney } from '../format'

export default function ChannelStack({ trend, tokens }: { trend: Trend | null; tokens: Tokens }) {
  const opt = useMemo<EChartsOption>(() => {
    const rows = trend?.channel ?? []
    return {
      animationDuration: 300,
      grid: { left: 56, right: 16, top: 34, bottom: 24 },
      legend: {
        top: 0, right: 0, icon: 'circle', itemWidth: 9, itemHeight: 9,
        textStyle: { color: tokens.sub, fontSize: 12 },
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: tokens.surface, borderColor: tokens.grid, textStyle: { color: tokens.ink, fontSize: 12 },
        valueFormatter: (v) => fmtTipMoney(v as number),
      },
      xAxis: {
        type: 'category', data: rows.map(r => r.dt.slice(5)),
        axisLine: { lineStyle: { color: tokens.baseline } }, axisTick: { show: false },
        axisLabel: { color: tokens.muted, fontSize: 11 },
      },
      yAxis: {
        type: 'value', splitLine: { lineStyle: { color: tokens.grid } },
        axisLabel: { color: tokens.muted, fontSize: 11, formatter: fmtAxisMoney },
      },
      series: CHANNELS.map((ch, i) => ({
        name: CHANNEL_LABEL[ch], type: 'bar' as const, stack: 'gmv', barWidth: '58%',
        itemStyle: { color: tokens.series[i], borderColor: tokens.surface, borderWidth: 1 },
        data: rows.map(r => Math.round(r[ch])),
      })),
    }
  }, [trend, tokens])

  return (
    <div className="panel">
      <h2>{t('归因渠道 GMV 结构', 'GMV by attributed channel')} <span className="note">{t('直播结束 30 分钟内支付归直播间', 'paid ≤30 min after stream ends attributes to the live room')}</span></h2>
      <Chart option={opt} height={374} />
    </div>
  )
}

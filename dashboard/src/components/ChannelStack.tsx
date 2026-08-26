import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import Chart from './Chart'
import type { Trend } from '../api'
import { CHANNELS, CHANNEL_LABEL, type Tokens } from '../theme'

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
        valueFormatter: (v) => ((v as number) / 1e4).toFixed(1) + ' 万元',
      },
      xAxis: {
        type: 'category', data: rows.map(r => r.dt.slice(5)),
        axisLine: { lineStyle: { color: tokens.baseline } }, axisTick: { show: false },
        axisLabel: { color: tokens.muted, fontSize: 11 },
      },
      yAxis: {
        type: 'value', splitLine: { lineStyle: { color: tokens.grid } },
        axisLabel: { color: tokens.muted, fontSize: 11, formatter: (v: number) => (v / 1e4).toFixed(0) + '万' },
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
      <h2>归因渠道 GMV 结构 <span className="note">直播结束 30 分钟内支付归直播间</span></h2>
      <Chart option={opt} height={374} />
    </div>
  )
}

import { useEffect, useMemo, useState } from 'react'
import { fetchBreakdown, fetchDrift, fetchMeta, fetchOverview, fetchTrend, type BreakdownRow, type Overview, type Trend } from './api'
import { TOKENS, type Mode } from './theme'
import { fmtCount, fmtHours, fmtMoney, fmtPct, fmtYuan } from './format'
import CaliberModal from './components/CaliberModal'
import GovernancePanel from './components/GovernancePanel'
import MetricCard, { type MetricDef } from './components/MetricCard'
import TrendPanel from './components/TrendPanel'
import ChannelStack from './components/ChannelStack'
import BreakdownTable from './components/BreakdownTable'

const METRICS: MetricDef[] = [
  { key: 'gmv', label: 'GMV', fmt: fmtMoney, unit: '元', goodDir: 1, caliber: '支付口径,剔秒退/测试/风控,外币按支付日汇率折算' },
  { key: 'pay_amt', label: '支付金额', fmt: fmtMoney, unit: '元', goodDir: 1, caliber: '支付成功流水金额(分→元)' },
  { key: 'pay_order_cnt', label: '支付订单数', fmt: fmtCount, unit: '单', goodDir: 1, caliber: '支付成功订单去重计数' },
  { key: 'pay_user_cnt', label: '支付人数', fmt: fmtCount, unit: '人', goodDir: 1, caliber: '期内支付成功用户去重' },
  { key: 'atv', label: '客单价', fmt: fmtYuan, goodDir: 1, caliber: '支付金额 ÷ 支付人数(按人)' },
  { key: 'refund_rate_14d', label: '近14天退款率', fmt: fmtPct, goodDir: -1, caliber: '退款到账距支付≤14天的退款金额 ÷ 支付金额' },
  { key: 'refund_amt_14d', label: '退款金额(14天口径)', fmt: fmtMoney, unit: '元', goodDir: -1, caliber: '打款金额缺失时回退申请金额' },
  { key: 'flash_refund_order_ratio', label: '秒退单占比', fmt: fmtPct, goodDir: -1, caliber: '支付后 60 秒内发起退款的订单占比' },
  { key: 'delivered_rate', label: '妥投率', fmt: fmtPct, goodDir: 1, caliber: '签收运单 ÷ 揽收运单(按运单去重)' },
  { key: 'avg_ship_hours', label: '平均发货时长', fmt: fmtHours, goodDir: -1, caliber: '支付→揽收,剔除预售单' },
  { key: 'new_user_cnt', label: '新客数', fmt: fmtCount, unit: '人', goodDir: 1, caliber: '历史首笔支付成功订单在当期(非注册口径)' },
  { key: 'new_user_gmv_ratio', label: '新客GMV占比', fmt: fmtPct, goodDir: 1, caliber: '新客 GMV ÷ 总 GMV' },
  { key: 'repurchase_rate', label: '复购率', fmt: fmtPct, goodDir: 1, caliber: '期内支付用户中历史第≥2单用户占比' },
  { key: 'live_gmv', label: '渠道归因GMV·直播', fmt: fmtMoney, unit: '元', goodDir: 1, caliber: '含直播结束 30 分钟内支付的延迟归因' },
]

const PRESETS = [
  { days: 7, label: '近7天' },
  { days: 30, label: '近30天' },
  { days: 90, label: '近90天' },
]

const addDays = (d: string, n: number) => {
  const t = new Date(d + 'T00:00:00')
  t.setDate(t.getDate() + n)
  const p = (x: number) => String(x).padStart(2, '0')
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}`
}

export default function App() {
  const [mode, setMode] = useState<Mode>(matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
  const [maxDt, setMaxDt] = useState<string | null>(null)
  const [days, setDays] = useState(30)
  const [ov, setOv] = useState<Overview | null>(null)
  const [trend, setTrend] = useState<Trend | null>(null)
  const [dim, setDim] = useState('channel')
  const [caliber, setCaliber] = useState<{ key: string; title: string } | null>(null)
  const [gov, setGov] = useState(false)
  const [driftByKey, setDriftByKey] = useState<Map<string, number>>(new Map())
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [bd, setBd] = useState<BreakdownRow[]>([])
  const tokens = TOKENS[mode]

  useEffect(() => {
    const mq = matchMedia('(prefers-color-scheme: dark)')
    const fn = () => setMode(mq.matches ? 'dark' : 'light')
    mq.addEventListener('change', fn)
    return () => mq.removeEventListener('change', fn)
  }, [])
  useEffect(() => { document.documentElement.dataset.mode = mode }, [mode])
  useEffect(() => {
    fetchMeta().then(m => setMaxDt(m.mx))
      .catch(e => setLoadErr(String(e.message ?? e)))
    fetchDrift().then(r => {
      const m = new Map<string, number>()
      r.events.forEach(e => m.set(e.metric_key, (m.get(e.metric_key) ?? 0) + 1))
      setDriftByKey(m)
    }).catch(() => setDriftByKey(new Map()))
  }, [])

  const range = useMemo(() => {
    if (!maxDt) return null
    return { start: addDays(maxDt, -(days - 1)), end: maxDt }
  }, [maxDt, days])

  useEffect(() => {
    if (!range) return
    const ac = new AbortController()
    setLoadErr(null)
    Promise.all([
      fetchOverview(range.start, range.end, ac.signal).then(setOv),
      fetchTrend(range.start, range.end, ac.signal).then(setTrend),
    ]).catch(e => { if (e.name !== 'AbortError') setLoadErr(String(e.message ?? e)) })
    return () => ac.abort()
  }, [range])
  useEffect(() => {
    if (!range) return
    const ac = new AbortController()
    fetchBreakdown(range.start, range.end, dim, ac.signal)
      .then(r => setBd(r.rows))
      .catch(e => { if (e.name !== 'AbortError') setLoadErr(String(e.message ?? e)) })
    return () => ac.abort()
  }, [range, dim])

  const cardMap = useMemo(() => new Map((ov?.cards ?? []).map(c => [c.key, c])), [ov])

  return (
    <div className="wrap">
      <div className="topbar">
        <h1>MetricLens · 业务大盘</h1>
        <span className="sub">电商四域数仓 ODS→APP 全链路 · 每个指标卡都是口径卡入口</span>
        <span className="spacer" />
        {range && <span className="range-note">{range.start} ~ {range.end} · 环比 {ov?.prev_start} ~ {ov?.prev_end}</span>}
        <div className="seg">
          {PRESETS.map(p => (
            <button key={p.days} className={p.days === days ? 'on' : ''} onClick={() => setDays(p.days)}>{p.label}</button>
          ))}
        </div>
        <button className="gov-btn" onClick={() => setGov(true)} title="重复建设清单 + 口径漂移事件流(指纹扫描 + LLM 仲裁)">治理台</button>
      </div>

      {loadErr && <div className="loading" role="alert">数据加载失败:{loadErr}(检查取数服务 8612 是否在线)</div>}
      {!ov && !loadErr ? <div className="loading">数据加载中…</div> : !ov ? null : (
        <>
          <div className="grid-cards">
            {METRICS.map(m => <MetricCard key={`${m.key}-${mode}`} def={m} card={cardMap.get(m.key)} tokens={tokens} driftCount={driftByKey.get(m.key) ?? 0} onCaliber={(k, t) => setCaliber({ key: k, title: t })} />)}
          </div>
          <div className="panel-row two">
            <TrendPanel key={`t-${mode}-${days}`} trend={trend} tokens={tokens} />
            <ChannelStack key={`c-${mode}-${days}`} trend={trend} tokens={tokens} />
          </div>
          <BreakdownTable dim={dim} onDim={setDim} rows={bd} />
        </>
      )}
      {caliber && <CaliberModal metricKey={caliber.key} title={caliber.title} tokens={tokens} onClose={() => setCaliber(null)} />}
      {gov && <GovernancePanel onClose={() => setGov(false)} />}
    </div>
  )
}

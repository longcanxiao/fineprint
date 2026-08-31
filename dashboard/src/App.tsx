import { useEffect, useMemo, useState } from 'react'
import { fetchBreakdown, fetchDrift, fetchMeta, fetchOverview, fetchTrend, type BreakdownRow, type Overview, type Trend } from './api'
import { TOKENS, type Mode } from './theme'
import { fmtCount, fmtHours, fmtMoney, fmtPct, fmtYuan } from './format'
import { EN, t } from './i18n'
import CaliberModal from './components/CaliberModal'
import GovernancePanel from './components/GovernancePanel'
import MetricCard, { type MetricDef } from './components/MetricCard'
import TrendPanel from './components/TrendPanel'
import ChannelStack from './components/ChannelStack'
import BreakdownTable from './components/BreakdownTable'

const METRICS: MetricDef[] = [
  { key: 'gmv', label: 'GMV', fmt: fmtMoney, unit: t('元', 'CNY'), goodDir: 1, caliber: t('支付口径,剔秒退/测试/风控,外币按支付日汇率折算', 'paid basis; excludes flash-refund/test/risk-control orders; FX at pay-date rate') },
  { key: 'pay_amt', label: t('支付金额', 'Payment amount'), fmt: fmtMoney, unit: t('元', 'CNY'), goodDir: 1, caliber: t('支付成功流水金额(分→元)', 'successful-payment amount (cents→yuan)') },
  { key: 'pay_order_cnt', label: t('支付订单数', 'Paid orders'), fmt: fmtCount, unit: t('单', 'orders'), goodDir: 1, caliber: t('支付成功订单去重计数', 'distinct successfully paid orders') },
  { key: 'pay_user_cnt', label: t('支付人数', 'Paying users'), fmt: fmtCount, unit: t('人', 'users'), goodDir: 1, caliber: t('期内支付成功用户去重', 'distinct paying users in period') },
  { key: 'atv', label: t('客单价', 'Avg order value'), fmt: fmtYuan, goodDir: 1, caliber: t('支付金额 ÷ 支付人数(按人)', 'payment amount ÷ paying users (per user)') },
  { key: 'refund_rate_14d', label: t('近14天退款率', '14-day refund rate'), fmt: fmtPct, goodDir: -1, caliber: t('退款到账距支付≤14天的退款金额 ÷ 支付金额', 'refunds landing ≤14 days after payment ÷ payment amount') },
  { key: 'refund_amt_14d', label: t('退款金额(14天口径)', 'Refund amount (14d)'), fmt: fmtMoney, unit: t('元', 'CNY'), goodDir: -1, caliber: t('打款金额缺失时回退申请金额', 'falls back to requested amount when payout amount is missing') },
  { key: 'flash_refund_order_ratio', label: t('秒退单占比', 'Flash-refund ratio'), fmt: fmtPct, goodDir: -1, caliber: t('支付后 60 秒内发起退款的订单占比', 'share of orders with refund requested within 60s of payment') },
  { key: 'delivered_rate', label: t('妥投率', 'Delivery success rate'), fmt: fmtPct, goodDir: 1, caliber: t('签收运单 ÷ 揽收运单(按运单去重)', 'signed-for waybills ÷ picked-up waybills (distinct waybills)') },
  { key: 'avg_ship_hours', label: t('平均发货时长', 'Avg hours to ship'), fmt: fmtHours, goodDir: -1, caliber: t('支付→揽收,剔除预售单', 'payment→carrier pickup; presale orders excluded') },
  { key: 'new_user_cnt', label: t('新客数', 'New customers'), fmt: fmtCount, unit: t('人', 'users'), goodDir: 1, caliber: t('历史首笔支付成功订单在当期(非注册口径)', 'first-ever successful payment falls in period (not registration)') },
  { key: 'new_user_gmv_ratio', label: t('新客GMV占比', 'New-customer GMV %'), fmt: fmtPct, goodDir: 1, caliber: t('新客 GMV ÷ 总 GMV', 'new-customer GMV ÷ total GMV') },
  { key: 'repurchase_rate', label: t('复购率', 'Repurchase rate'), fmt: fmtPct, goodDir: 1, caliber: t('期内支付用户中历史第≥2单用户占比', 'share of paying users on their ≥2nd lifetime order') },
  { key: 'live_gmv', label: t('渠道归因GMV·直播', 'Live-attributed GMV'), fmt: fmtMoney, unit: t('元', 'CNY'), goodDir: 1, caliber: t('含直播结束 30 分钟内支付的延迟归因', 'includes delayed attribution: paid ≤30 min after stream ends') },
]

const PRESETS = [
  { days: 7, label: t('近7天', '7d') },
  { days: 30, label: t('近30天', '30d') },
  { days: 90, label: t('近90天', '90d') },
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
    if (EN) {
      document.documentElement.lang = 'en'
      document.title = 'FinePrint · Business Overview'
    }
  }, [])
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
        <h1>{t('FinePrint · 业务大盘', 'FinePrint · Business Overview')}</h1>
        <span className="sub">{t('电商四域数仓 ODS→APP 全链路 · 每个指标卡都是口径卡入口', 'E-commerce warehouse, full ODS→APP lineage · every metric card opens its definition card')}</span>
        <span className="spacer" />
        {range && <span className="range-note">{range.start} ~ {range.end} · {t('环比', 'vs prev')} {ov?.prev_start} ~ {ov?.prev_end}</span>}
        <div className="seg">
          {PRESETS.map(p => (
            <button key={p.days} className={p.days === days ? 'on' : ''} onClick={() => setDays(p.days)}>{p.label}</button>
          ))}
        </div>
        <button className="gov-btn" onClick={() => setGov(true)} title={t('重复建设清单 + 口径漂移事件流(指纹扫描 + LLM 仲裁)', 'Duplicate-build inventory + definition drift events (fingerprint scan + LLM arbitration)')}>{t('治理台', 'Governance')}</button>
      </div>

      {loadErr && <div className="loading" role="alert">{t('数据加载失败:', 'Failed to load data: ')}{loadErr}{t('(检查取数服务 8612 是否在线)', ' (check that the data service on port 8612 is up)')}</div>}
      {!ov && !loadErr ? <div className="loading">{t('数据加载中…', 'Loading…')}</div> : !ov ? null : (
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

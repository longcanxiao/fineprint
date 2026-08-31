import type { BreakdownRow } from '../api'
import { CHANNEL_LABEL } from '../theme'
import { fmtMoney, fmtPct } from '../format'
import { t } from '../i18n'

const DIMS = [
  { key: 'channel', label: t('归因渠道', 'Channel') },
  { key: 'category', label: t('类目', 'Category') },
  { key: 'province', label: t('省份', 'Province') },
  { key: 'live_room', label: t('直播间', 'Live room') },
]

export default function BreakdownTable({ dim, onDim, rows }: {
  dim: string; onDim: (d: string) => void; rows: BreakdownRow[]
}) {
  const nameOf = (r: BreakdownRow) =>
    dim === 'channel' ? (CHANNEL_LABEL[r.name] ?? r.name) : dim === 'live_room' ? t(`直播间 ${r.name}`, `Live room ${r.name}`) : r.name
  return (
    <div className="panel">
      <h2>
        {t('维度明细', 'Dimension detail')}
        <span className="note">{t('按 GMV 降序 · Top 30', 'sorted by GMV desc · top 30')}</span>
        <span className="dimtabs">
          {DIMS.map(d => (
            <button key={d.key} className={dim === d.key ? 'on' : ''} onClick={() => onDim(d.key)}>{d.label}</button>
          ))}
        </span>
      </h2>
      <div style={{ overflowX: 'auto' }}>
        <table className="bd">
          <thead>
            <tr><th>{DIMS.find(d => d.key === dim)?.label}</th><th>GMV</th><th>{t('GMV 份额', 'GMV share')}</th><th>{t('支付金额', 'Payment amt')}</th><th>{t('支付订单数', 'Paid orders')}</th><th>{t('秒退单占比', 'Flash-refund %')}</th></tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.name}>
                <td>{nameOf(r)}</td>
                <td>{fmtMoney(r.gmv)}</td>
                <td>
                  <span className="sharebar">
                    <span className="track"><span className="fill" style={{ width: `${Math.min(100, r.share * 100)}%` }} /></span>
                    {fmtPct(r.share, 1)}
                  </span>
                </td>
                <td>{fmtMoney(r.pay_amt)}</td>
                <td>{r.pay_order_cnt.toLocaleString()}</td>
                <td>{fmtPct(r.flash_ratio)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

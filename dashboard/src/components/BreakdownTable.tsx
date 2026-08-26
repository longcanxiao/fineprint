import type { BreakdownRow } from '../api'
import { CHANNEL_LABEL } from '../theme'
import { fmtMoney, fmtPct } from '../format'

const DIMS = [
  { key: 'channel', label: '归因渠道' },
  { key: 'category', label: '类目' },
  { key: 'province', label: '省份' },
  { key: 'live_room', label: '直播间' },
]

export default function BreakdownTable({ dim, onDim, rows }: {
  dim: string; onDim: (d: string) => void; rows: BreakdownRow[]
}) {
  const nameOf = (r: BreakdownRow) =>
    dim === 'channel' ? (CHANNEL_LABEL[r.name] ?? r.name) : dim === 'live_room' ? `直播间 ${r.name}` : r.name
  return (
    <div className="panel">
      <h2>
        维度明细
        <span className="note">按 GMV 降序 · Top 30</span>
        <span className="dimtabs">
          {DIMS.map(d => (
            <button key={d.key} className={dim === d.key ? 'on' : ''} onClick={() => onDim(d.key)}>{d.label}</button>
          ))}
        </span>
      </h2>
      <div style={{ overflowX: 'auto' }}>
        <table className="bd">
          <thead>
            <tr><th>{DIMS.find(d => d.key === dim)?.label}</th><th>GMV</th><th>GMV 份额</th><th>支付金额</th><th>支付订单数</th><th>秒退单占比</th></tr>
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

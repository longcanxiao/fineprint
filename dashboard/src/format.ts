export const fmtMoney = (v: number | null | undefined): string =>
  v == null ? '–' : v >= 1e8 ? (v / 1e8).toFixed(2) + ' 亿' : v >= 1e4 ? (v / 1e4).toFixed(1) + ' 万' : v.toFixed(0)

export const fmtCount = (v: number | null | undefined): string =>
  v == null ? '–' : v >= 1e8 ? (v / 1e8).toFixed(2) + ' 亿' : v >= 1e4 ? (v / 1e4).toFixed(1) + ' 万' : String(Math.round(v))

export const fmtPct = (v: number | null | undefined, d = 2): string =>
  v == null ? '–' : (v * 100).toFixed(d) + '%'

export const fmtHours = (v: number | null | undefined): string =>
  v == null ? '–' : v.toFixed(1) + ' h'

export const fmtYuan = (v: number | null | undefined): string =>
  v == null ? '–' : v.toFixed(0) + ' 元'

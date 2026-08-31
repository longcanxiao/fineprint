import { EN } from './i18n'

// 中文单位:亿/万;英文单位:B/M/K(数据为人民币,金额前缀 ¥ 由 fmtYuan/单位标签承担)
const zhBig = (v: number, intBelow1e4 = false): string =>
  v >= 1e8 ? (v / 1e8).toFixed(2) + ' 亿' : v >= 1e4 ? (v / 1e4).toFixed(1) + ' 万' : intBelow1e4 ? String(Math.round(v)) : v.toFixed(0)

const enBig = (v: number): string =>
  v >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : String(Math.round(v))

export const fmtMoney = (v: number | null | undefined): string =>
  v == null ? '–' : EN ? enBig(v) : zhBig(v)

export const fmtCount = (v: number | null | undefined): string =>
  v == null ? '–' : EN ? enBig(v) : zhBig(v, true)

export const fmtPct = (v: number | null | undefined, d = 2): string =>
  v == null ? '–' : (v * 100).toFixed(d) + '%'

export const fmtHours = (v: number | null | undefined): string =>
  v == null ? '–' : v.toFixed(1) + ' h'

export const fmtYuan = (v: number | null | undefined): string =>
  v == null ? '–' : EN ? '¥' + v.toFixed(0) : v.toFixed(0) + ' 元'

// ECharts 金额轴刻度/悬浮提示(GMV 日级量级:中文按万,英文按 M/K)
export const fmtAxisMoney = (v: number): string =>
  EN ? (v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : (v / 1e3).toFixed(0) + 'K') : (v / 1e4).toFixed(0) + '万'

export const fmtTipMoney = (v: number): string =>
  EN ? '¥' + (v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : (v / 1e3).toFixed(1) + 'K') : (v / 1e4).toFixed(1) + ' 万元'

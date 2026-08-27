export interface Card { key: string; value: number | null; prev: number | null; spark: { dt: string; v: number | null }[] }
export interface Overview { start: string; end: string; prev_start: string; prev_end: string; cards: Card[] }
export interface TrendRow { dt: string; gmv: number; refund_rate_14d: number | null }
export interface ChannelRow { dt: string; app: number; h5: number; live: number }
export interface Trend { daily: TrendRow[]; channel: ChannelRow[] }
export interface BreakdownRow { name: string; gmv: number; pay_amt: number; pay_order_cnt: number; flash_ratio: number | null; share: number }

const get = async <T,>(url: string, signal?: AbortSignal): Promise<T> => {
  const r = await fetch(url, { signal })
  if (!r.ok) throw new Error(`${url}: ${r.status}`)
  return r.json()
}

export const fetchMeta = (signal?: AbortSignal) => get<{ mn: string; mx: string }>(`/api/meta`, signal)
export const fetchOverview = (s: string, e: string, signal?: AbortSignal) =>
  get<Overview>(`/api/overview?start=${s}&end=${e}`, signal)
export const fetchTrend = (s: string, e: string, signal?: AbortSignal) =>
  get<Trend>(`/api/trend?start=${s}&end=${e}`, signal)
export const fetchBreakdown = (s: string, e: string, dim: string, signal?: AbortSignal) =>
  get<{ rows: BreakdownRow[] }>(`/api/breakdown?start=${s}&end=${e}&dim=${dim}`, signal)

export interface DriftEvent {
  detected_at: string; metric_key: string; kind: string; severity: 'high' | 'medium' | 'info'
  detail: Record<string, string | undefined>
}
export interface GovPairFull { a: string; b: string; fingerprint: string; tier: 'A' | 'B'; verdict: string; reason?: string; suggestion?: string }
export interface GovFamily { a: string; b: string; fingerprint: string; grain_a: string[]; grain_b: string[] }
export interface GovReport {
  generated_at: string | null; llm_model?: string
  a_tier_pairs?: number; a_tier_dup?: number; a_tier_agg_distinct?: number
  b_tier_pairs?: number; b_tier_skipped?: number
  duplicates: GovPairFull[]; distinct: GovPairFull[]; families?: GovFamily[]
}
export interface LineageGraph {
  target: string
  nodes: { id: string; layer: string }[]
  edges: { source: string; target: string }[]
  sources: string[]
}

export const fetchDrift = (metricKey?: string, signal?: AbortSignal) =>
  get<{ events: DriftEvent[]; total: number }>(`/api/governance/drift${metricKey ? `?metric_key=${metricKey}` : ''}`, signal)
export const fetchGovReport = (signal?: AbortSignal) => get<GovReport>(`/api/governance/report`, signal)
export const fetchLineageGraph = (model: string, column: string, signal?: AbortSignal) =>
  get<LineageGraph>(`/api/lineage/graph/${model}/${column}`, signal)

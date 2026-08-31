import { t } from './i18n'

export type Mode = 'light' | 'dark'

export interface Tokens {
  surface: string; page: string; ink: string; sub: string; muted: string
  grid: string; baseline: string; border: string
  series: string[]; good: string; bad: string
}

export const TOKENS: Record<Mode, Tokens> = {
  light: {
    surface: '#fcfcfb', page: '#f9f9f7', ink: '#0b0b0b', sub: '#52514e', muted: '#898781',
    grid: '#e1e0d9', baseline: '#c3c2b7', border: 'rgba(11,11,11,0.10)',
    series: ['#2a78d6', '#eb6834', '#1baf7a'], good: '#006300', bad: '#d03b3b',
  },
  dark: {
    surface: '#1a1a19', page: '#0d0d0d', ink: '#ffffff', sub: '#c3c2b7', muted: '#898781',
    grid: '#2c2c2a', baseline: '#383835', border: 'rgba(255,255,255,0.10)',
    series: ['#3987e5', '#d95926', '#199e70'], good: '#0ca30c', bad: '#d03b3b',
  },
}

export const CHANNELS = ['app', 'h5', 'live'] as const
export const CHANNEL_LABEL: Record<string, string> = { app: 'App', h5: 'H5', live: t('直播间', 'Live room') }

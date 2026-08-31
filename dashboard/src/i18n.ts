// 看板演示层双语开关:?lang=en 切英文,默认中文。
// 与产品 CLI 的 fineprint/i18n.py 同一约定:t(zh, en)。
export const EN = new URLSearchParams(location.search).get('lang') === 'en'

export const t = (zh: string, en: string): string => (EN ? en : zh)

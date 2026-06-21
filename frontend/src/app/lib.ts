// API ベースURL（環境変数優先・既定はローカルbackend）
export const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001'

// --- スコアAPI (/api/v1/score/{race_id}) の型 ---

export type CategoryScore = {
  total: number
  details: Record<string, number>
}

export type SireInfo = {
  name: string
  hanshoku_bango: string
  win_rate: number
  roi: number
}

export type Prediction = {
  horse_number: number
  horse_name: string
  ketto_toroku_bango: string
  waku: number
  sei_rei: string
  keiro: string
  kishu: string
  futan: number
  chokyoshi: string
  banushi: string
  seisansha: string
  odds: number
  popularity: number
  total_score: number
  category_scores: Partial<Record<'A' | 'B' | 'C' | 'D' | 'E', CategoryScore>>
  sire_info: SireInfo | null
  bms_info: SireInfo | null
}

export type RaceData = {
  race_id: string
  race_name: string
  predictions: Prediction[]
}

// --- カレンダーAPI (/api/v1/calendar/*) の型 ---

export type KaisaiDate = {
  date: string
  display: string
  venues: string[]
  race_count: number
}

export type RaceListItem = {
  race_id: string
  keibajo: string
  race_bango: number
  race_name: string
  surface: string
  kyori: number
  shusso_tosu: number
  hasso: string
}

// 一覧に表示するカテゴリ列（D=スピードは新馬戦では常に0なので「-」表示）
export const CATEGORY_COLS: { key: 'A' | 'B' | 'C' | 'D' | 'E'; label: string }[] = [
  { key: 'A', label: 'A 血統' },
  { key: 'B', label: 'B 条件' },
  { key: 'C', label: 'C 陣営' },
  { key: 'D', label: 'D 速度' },
  { key: 'E', label: 'E 枠等' },
]

// JRA 枠番の色
export const WAKU_COLORS: Record<number, string> = {
  1: 'bg-white text-black',
  2: 'bg-black text-white border border-gray-600',
  3: 'bg-red-600 text-white',
  4: 'bg-blue-600 text-white',
  5: 'bg-yellow-400 text-black',
  6: 'bg-green-600 text-white',
  7: 'bg-orange-500 text-black',
  8: 'bg-pink-400 text-black',
}

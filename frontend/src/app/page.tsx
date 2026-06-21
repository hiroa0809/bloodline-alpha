"use client"

import { useState, useEffect } from 'react'

// --- 実API (/api/v1/score/{race_id}) のレスポンス型 ---

type CategoryScore = {
  total: number;
  details: Record<string, number>;
}

type SireInfo = {
  name: string;
  hanshoku_bango: string;
  win_rate: number;
  roi: number;
}

type Prediction = {
  horse_number: number;
  horse_name: string;
  ketto_toroku_bango: string;
  // 出馬表情報
  waku: number;
  sei_rei: string;     // 性齢（例: 牡2）
  keiro: string;       // 毛色
  kishu: string;       // 騎手名
  futan: number;       // 斤量(kg)
  chokyoshi: string;   // 調教師（所属付き）
  banushi: string;     // 馬主
  seisansha: string;   // 生産者
  odds: number;
  popularity: number;
  total_score: number;
  category_scores: Record<'A' | 'B' | 'C' | 'D' | 'E', CategoryScore>;
  sire_info: SireInfo | null;  // #11 馬詳細で使用
  bms_info: SireInfo | null;   // #11 馬詳細で使用
}

type RaceData = {
  race_id: string;
  race_name: string;
  predictions: Prediction[];
}

// 一覧に表示するカテゴリ列（D=スピードは新馬戦では常に0なので「-」表示）
const CATEGORY_COLS: { key: 'A' | 'B' | 'C' | 'D' | 'E'; label: string }[] = [
  { key: 'A', label: 'A 血統' },
  { key: 'B', label: 'B 条件' },
  { key: 'C', label: 'C 陣営' },
  { key: 'D', label: 'D 速度' },
  { key: 'E', label: 'E 枠等' },
]

// JRA 枠番の色
const WAKU_COLORS: Record<number, string> = {
  1: 'bg-white text-black',
  2: 'bg-black text-white border border-gray-600',
  3: 'bg-red-600 text-white',
  4: 'bg-blue-600 text-white',
  5: 'bg-yellow-400 text-black',
  6: 'bg-green-600 text-white',
  7: 'bg-orange-500 text-black',
  8: 'bg-pink-400 text-black',
}

export default function Home() {
  const [raceId, setRaceId] = useState('2024010608010104')
  const [data, setData] = useState<RaceData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState('')

  const fetchScore = async () => {
    if (!raceId) return
    setLoading(true)
    setError('')
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001'
      const res = await fetch(`${apiUrl}/api/v1/score/${raceId}`)
      if (!res.ok) {
        if (res.status === 404) throw new Error('該当レースが見つかりません (race_id を確認してください)')
        throw new Error(`API エラー (${res.status})`)
      }
      const json: RaceData = await res.json()
      setData(json)
      setUpdatedAt(new Date().toLocaleTimeString())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'データ取得に失敗しました')
      setData(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchScore()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <main className="min-h-screen bg-gray-950 text-gray-200 p-6 font-sans">
      <div className="max-w-[1400px] mx-auto">
        <header className="mb-6 flex flex-col md:flex-row md:items-center justify-between">
          <div>
            <h1 className="text-3xl font-extrabold tracking-tight text-white mb-1">
              Bloodline <span className="text-blue-500">Alpha</span>
            </h1>
            <p className="text-gray-400 text-sm">JRA 新馬戦 血統期待値ダッシュボード</p>
          </div>
          <div className="mt-4 md:mt-0 flex gap-3">
            <input
              type="text"
              value={raceId}
              onChange={(e) => setRaceId(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') fetchScore() }}
              placeholder="レースID (16桁)"
              className="bg-gray-900 border border-gray-700 rounded-md px-4 py-2 text-sm focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all shadow-sm"
            />
            <button
              onClick={fetchScore}
              disabled={loading}
              className="bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 disabled:text-gray-400 text-white px-6 py-2 rounded-md text-sm font-semibold shadow-md transition-all active:scale-95"
            >
              {loading ? '分析中...' : '再計算'}
            </button>
          </div>
        </header>

        {error && (
          <div className="bg-red-500/10 border-l-4 border-red-500 text-red-400 px-4 py-3 rounded mb-6 text-sm">
            {error}
          </div>
        )}

        {data && (
          <div className="bg-gray-900/50 rounded-xl shadow-2xl overflow-hidden border border-gray-800">
            {/* レース名（新馬戦は競走名が空のことが多いためフォールバック） */}
            <div className="bg-gray-900 px-6 py-4 border-b border-gray-800">
              <h2 className="text-lg font-bold text-white">
                {data.race_name?.trim() || '（新馬戦・レース名なし）'}
              </h2>
              <p className="text-xs text-gray-500 mt-0.5">{data.predictions.length} 頭立て・総合スコア降順</p>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse text-sm">
                <thead>
                  <tr className="bg-gray-900 text-gray-400 text-xs uppercase tracking-wider border-b border-gray-800">
                    <th className="px-3 py-3 font-semibold">枠/馬番</th>
                    <th className="px-3 py-3 font-semibold">出走馬</th>
                    <th className="px-3 py-3 font-semibold">騎手 / 斤量</th>
                    <th className="px-3 py-3 font-semibold">調教師</th>
                    <th className="px-3 py-3 font-semibold">馬主 / 生産者</th>
                    <th className="px-3 py-3 font-semibold whitespace-nowrap">オッズ (人気)</th>
                    <th className="px-3 py-3 font-semibold min-w-[180px]">総合スコア</th>
                    {CATEGORY_COLS.map((c) => (
                      <th key={c.key} className="px-2 py-3 font-semibold text-right whitespace-nowrap">{c.label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800/60">
                  {data.predictions.map((p) => (
                    <tr key={p.ketto_toroku_bango || p.horse_number} className="transition-colors hover:bg-gray-800/80">
                      {/* 枠（色）+ 馬番 */}
                      <td className="px-3 py-3 whitespace-nowrap">
                        <div className="flex items-center gap-2">
                          <span className={`inline-flex items-center justify-center w-6 h-6 rounded-sm font-bold text-xs ${WAKU_COLORS[p.waku] || 'bg-gray-700 text-gray-200'}`}>
                            {p.waku || '-'}
                          </span>
                          <span className="font-mono font-bold text-gray-100">{p.horse_number}</span>
                        </div>
                      </td>
                      {/* 馬名 / 性齢・毛色 */}
                      <td className="px-3 py-3 whitespace-nowrap">
                        <div className="font-bold text-gray-100">{p.horse_name}</div>
                        <div className="text-xs text-gray-500 mt-0.5">
                          {[p.sei_rei, p.keiro].filter(Boolean).join('・') || '----'}
                        </div>
                      </td>
                      {/* 騎手 / 斤量 */}
                      <td className="px-3 py-3 whitespace-nowrap">
                        <div className="text-gray-200">{p.kishu || '----'}</div>
                        <div className="text-xs text-gray-500 mt-0.5">{p.futan ? `${p.futan.toFixed(1)}kg` : '-'}</div>
                      </td>
                      {/* 調教師 */}
                      <td className="px-3 py-3 whitespace-nowrap text-gray-300">{p.chokyoshi || '----'}</td>
                      {/* 馬主 / 生産者 */}
                      <td className="px-3 py-3 whitespace-nowrap">
                        <div className="text-gray-300 text-xs">{p.banushi || '----'}</div>
                        <div className="text-xs text-gray-500 mt-0.5">{p.seisansha || '----'}</div>
                      </td>
                      {/* オッズ(人気) */}
                      <td className="px-3 py-3 whitespace-nowrap">
                        <div className="font-mono text-gray-200">{p.odds.toFixed(1)}</div>
                        <div className="text-xs text-gray-500 mt-0.5">{p.popularity || '-'}番人気</div>
                      </td>
                      {/* 総合スコア + バー */}
                      <td className="px-3 py-3 min-w-[180px]">
                        <div className="flex items-end gap-2 mb-1.5">
                          <span className="font-mono text-lg font-bold text-white">{p.total_score.toFixed(1)}</span>
                          <span className="text-xs text-gray-500 mb-0.5">/ 100</span>
                        </div>
                        <div className="w-full h-2 bg-gray-800 rounded-full overflow-hidden shadow-inner">
                          <div className="h-full bg-blue-500" style={{ width: `${Math.min(p.total_score, 100)}%` }} />
                        </div>
                      </td>
                      {/* A〜E 小計 */}
                      {CATEGORY_COLS.map((c) => {
                        const cat = p.category_scores[c.key]
                        const display = c.key === 'D' ? '-' : (cat ? cat.total.toFixed(1) : '-')
                        return (
                          <td key={c.key} className="px-2 py-3 text-right font-mono text-gray-300 whitespace-nowrap">
                            {display}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="bg-gray-900 p-4 border-t border-gray-800 flex justify-between items-center text-xs text-gray-500">
              <div>TARGET RACE: <span className="font-mono text-gray-400 bg-gray-800 px-2 py-1 rounded ml-1">{data.race_id}</span></div>
              <div>UPDATED: {updatedAt}</div>
            </div>
          </div>
        )}
      </div>
    </main>
  )
}

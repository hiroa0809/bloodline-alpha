"use client"

import { useState, useEffect } from 'react'

type ScoreDetails = {
  bloodline: number;
  condition: number;
  human: number;
}

type Prediction = {
  horse_id: string;
  horse_number: number;
  horse_name: string;
  score: number;
  score_details: ScoreDetails;
  odds: number;
  popularity: number;
  expected_value: number;
}

type RaceData = {
  race_id: string;
  predictions: Prediction[];
}

export default function Home() {
  const [raceId, setRaceId] = useState('202448013107')
  const [data, setData] = useState<RaceData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const fetchScore = async () => {
    if (!raceId) return
    setLoading(true)
    setError('')
    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
      const res = await fetch(`${apiUrl}/api/v1/score/mock/${raceId}`)
      if (!res.ok) throw new Error('API request failed')
      const json = await res.json()
      setData(json)
    } catch (err: any) {
      setError(err.message || 'Error fetching data')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchScore()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <main className="min-h-screen bg-gray-950 text-gray-200 p-8 font-sans">
      <div className="max-w-6xl mx-auto">
        <header className="mb-8 flex flex-col md:flex-row md:items-center justify-between">
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
              placeholder="レースID (例: 2024...)"
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
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="bg-gray-900 text-gray-400 text-xs uppercase tracking-widest border-b border-gray-800">
                    <th className="px-6 py-4 font-semibold">馬番</th>
                    <th className="px-6 py-4 font-semibold">出走馬</th>
                    <th className="px-6 py-4 font-semibold">オッズ (人気)</th>
                    <th className="px-6 py-4 font-semibold">総合スコア & 内訳</th>
                    <th className="px-6 py-4 font-semibold text-right">推定期待値</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800/60">
                  {data.predictions.map((p, i) => {
                    // 期待値が1.0を超えればプラス、下回ればマイナス見込み
                    const isHighEV = p.expected_value >= 1.0;

                    return (
                      <tr
                        key={p.horse_id || i}
                        className="transition-colors hover:bg-gray-800/80 cursor-pointer group"
                        title="クリックで血統詳細・算出根拠を表示 (開発中)"
                      >
                        <td className="px-6 py-4 whitespace-nowrap">
                          <span className={`inline-flex items-center justify-center w-7 h-7 rounded-sm font-bold text-sm
                            ${p.horse_number % 2 === 0 ? 'bg-gray-700 text-gray-200' : 'bg-gray-200 text-gray-800'} shadow-sm`}>
                            {p.horse_number}
                          </span>
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <div className="font-bold text-gray-100">{p.horse_name}</div>
                          <div className="text-xs text-gray-500 mt-1">ID: {p.horse_id?.slice(-4) || '----'}</div>
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap">
                          <div className="font-mono text-gray-200">{p.odds.toFixed(1)}</div>
                          <div className="text-xs text-gray-500 mt-1">{p.popularity || '-'}番人気</div>
                        </td>
                        <td className="px-6 py-4 min-w-[300px]">
                          <div className="flex items-end gap-3 mb-2">
                            <span className="font-mono text-xl font-bold text-white">{p.score.toFixed(1)}</span>
                            <span className="text-xs text-gray-500 mb-1">/ 100</span>
                          </div>
                          {/* スコア内訳の可視化バー */}
                          <div className="w-full h-2.5 bg-gray-800 rounded-full overflow-hidden flex shadow-inner">
                            <div className="h-full bg-blue-500" style={{ width: `${p.score_details.bloodline}%` }} title={`血統: ${p.score_details.bloodline.toFixed(1)}`} />
                            <div className="h-full bg-emerald-500" style={{ width: `${p.score_details.condition}%` }} title={`適性: ${p.score_details.condition.toFixed(1)}`} />
                            <div className="h-full bg-amber-500" style={{ width: `${p.score_details.human}%` }} title={`陣営: ${p.score_details.human.toFixed(1)}`} />
                          </div>
                          <div className="flex gap-4 mt-2 text-[10px] text-gray-500 font-medium">
                            <span className="flex items-center gap-1"><div className="w-2 h-2 rounded-full bg-blue-500"></div>血統</span>
                            <span className="flex items-center gap-1"><div className="w-2 h-2 rounded-full bg-emerald-500"></div>適性</span>
                            <span className="flex items-center gap-1"><div className="w-2 h-2 rounded-full bg-amber-500"></div>陣営</span>
                          </div>
                        </td>
                        <td className="px-6 py-4 whitespace-nowrap text-right">
                          <div className={`inline-flex flex-col items-end`}>
                            <span className={`font-mono text-2xl font-black tracking-tighter ${isHighEV ? 'text-emerald-400 drop-shadow-[0_0_8px_rgba(52,211,153,0.3)]' : 'text-gray-400'
                              }`}>
                              {p.expected_value.toFixed(2)}
                            </span>
                            <span className="text-xs text-gray-500 mt-1">回収見込</span>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            <div className="bg-gray-900 p-4 border-t border-gray-800 flex justify-between items-center text-xs text-gray-500">
              <div>TARGET RACE: <span className="font-mono text-gray-400 bg-gray-800 px-2 py-1 rounded ml-1">{data.race_id}</span></div>
              <div>UPDATED: {new Date().toLocaleTimeString()}</div>
            </div>
          </div>
        )}
      </div>
    </main>
  )
}

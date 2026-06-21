"use client"

import { useState, useEffect } from 'react'
import { API_BASE, RaceListItem } from '../lib'

// レース選択画面（#10c）— 指定開催日の新馬戦を場・R番号順に一覧表示
export default function CalendarRaces({
  date,
  display,
  onSelect,
  onBack,
}: {
  date: string
  display: string
  onSelect: (raceId: string) => void
  onBack: () => void
}) {
  const [races, setRaces] = useState<RaceListItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let aborted = false
    const load = async () => {
      setLoading(true)
      setError('')
      try {
        const res = await fetch(`${API_BASE}/api/v1/calendar/races?date=${date}`)
        if (!res.ok) throw new Error(`API エラー (${res.status})`)
        const json: RaceListItem[] = await res.json()
        if (!aborted) setRaces(json)
      } catch (err) {
        if (!aborted) setError(err instanceof Error ? err.message : 'レースの取得に失敗しました')
      } finally {
        if (!aborted) setLoading(false)
      }
    }
    load()
    return () => { aborted = true }
  }, [date])

  return (
    <div className="bg-gray-900/50 rounded-xl shadow-2xl overflow-hidden border border-gray-800">
      <div className="bg-gray-900 px-6 py-4 border-b border-gray-800 flex items-center gap-3">
        <button
          onClick={onBack}
          className="text-gray-400 hover:text-white text-sm bg-gray-800 hover:bg-gray-700 px-3 py-1.5 rounded-md transition-colors"
        >
          ‹ 開催日
        </button>
        <div>
          <h2 className="text-lg font-bold text-white">{display} のレースを選択</h2>
          <p className="text-xs text-gray-500 mt-0.5">新馬戦のみ</p>
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border-l-4 border-red-500 text-red-400 px-4 py-3 m-4 rounded text-sm">
          {error}
        </div>
      )}

      {loading ? (
        <div className="px-6 py-8 text-center text-gray-500 text-sm">読込中...</div>
      ) : (
        <ul className="divide-y divide-gray-800/60">
          {races.map((r) => (
            <li key={r.race_id}>
              <button
                onClick={() => onSelect(r.race_id)}
                className="w-full flex items-center justify-between px-6 py-4 text-left hover:bg-gray-800/80 transition-colors"
              >
                <div className="flex items-center gap-4">
                  <span className="font-mono font-bold text-blue-400 w-16">{r.keibajo}{r.race_bango}R</span>
                  <div>
                    <div className="font-bold text-gray-100">{r.race_name || '新馬'}</div>
                    <div className="text-xs text-gray-500 mt-0.5">
                      {[r.surface ? `${r.surface}${r.kyori}m` : '', r.shusso_tosu ? `${r.shusso_tosu}頭` : ''].filter(Boolean).join('・')}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  {r.hasso && <span className="font-mono text-sm text-gray-400">{r.hasso} 発走</span>}
                  <span className="text-gray-600">›</span>
                </div>
              </button>
            </li>
          ))}
          {races.length === 0 && (
            <li className="px-6 py-8 text-center text-gray-500 text-sm">この日の新馬戦はありません</li>
          )}
        </ul>
      )}
    </div>
  )
}

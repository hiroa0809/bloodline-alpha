"use client"

import { useState, useEffect, useCallback } from 'react'
import { API_BASE, KaisaiDate } from '../lib'

const PAGE_SIZE = 30

// 開催日選択画面（#10d）— 新馬戦のある開催日を新しい順に一覧表示
export default function CalendarDates({
  onSelect,
}: {
  onSelect: (date: string, display: string) => void
}) {
  const [dates, setDates] = useState<KaisaiDate[]>([])
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [hasMore, setHasMore] = useState(true)

  const load = useCallback(async (off: number) => {
    if (loading) return
    setLoading(true)
    setError('')
    try {
      const res = await fetch(`${API_BASE}/api/v1/calendar/dates?limit=${PAGE_SIZE}&offset=${off}`)
      if (!res.ok) throw new Error(`API エラー (${res.status})`)
      const json: KaisaiDate[] = await res.json()
      setDates((prev) => (off === 0 ? json : [...prev, ...json]))
      setHasMore(json.length === PAGE_SIZE)
      setOffset(off + json.length)
    } catch (err) {
      setError(err instanceof Error ? err.message : '開催日の取得に失敗しました')
    } finally {
      setLoading(false)
    }
  }, [loading])

  useEffect(() => {
    load(0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="bg-gray-900/50 rounded-xl shadow-2xl overflow-hidden border border-gray-800">
      <div className="bg-gray-900 px-6 py-4 border-b border-gray-800">
        <h2 className="text-lg font-bold text-white">開催日を選択</h2>
        <p className="text-xs text-gray-500 mt-0.5">新馬戦のある開催日（新しい順）</p>
      </div>

      {error && (
        <div className="bg-red-500/10 border-l-4 border-red-500 text-red-400 px-4 py-3 m-4 rounded text-sm">
          {error}
        </div>
      )}

      <ul className="divide-y divide-gray-800/60">
        {dates.map((d) => (
          <li key={d.date}>
            <button
              onClick={() => onSelect(d.date, d.display)}
              className="w-full flex items-center justify-between px-6 py-4 text-left hover:bg-gray-800/80 transition-colors"
            >
              <div>
                <div className="font-mono font-bold text-gray-100">{d.display}</div>
                <div className="text-xs text-gray-500 mt-0.5">{d.venues.join('・') || '－'}</div>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-sm text-gray-400">{d.race_count} 新馬戦</span>
                <span className="text-gray-600">›</span>
              </div>
            </button>
          </li>
        ))}
      </ul>

      <div className="p-4 border-t border-gray-800 text-center">
        {hasMore ? (
          <button
            onClick={() => load(offset)}
            disabled={loading}
            className="bg-gray-800 hover:bg-gray-700 disabled:text-gray-500 text-gray-200 px-6 py-2 rounded-md text-sm font-semibold transition-all"
          >
            {loading ? '読込中...' : 'もっと見る'}
          </button>
        ) : (
          <span className="text-xs text-gray-600">{dates.length} 件（これ以上ありません）</span>
        )}
      </div>
    </div>
  )
}

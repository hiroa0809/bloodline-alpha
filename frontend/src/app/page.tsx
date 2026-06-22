"use client"

import { useState } from 'react'
import { API_BASE, RaceData } from './lib'
import CalendarDates from './components/CalendarDates'
import CalendarRaces from './components/CalendarRaces'
import ScoreTable from './components/ScoreTable'

type View = 'dates' | 'races' | 'score'

export default function Home() {
  const [view, setView] = useState<View>('dates')
  const [selDate, setSelDate] = useState('')
  const [selDisplay, setSelDisplay] = useState('')
  const [data, setData] = useState<RaceData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState('')

  // 開催日 → レース選択へ
  const handleSelectDate = (date: string, display: string) => {
    setSelDate(date)
    setSelDisplay(display)
    setView('races')
  }

  // レース選択 → スコア取得して出馬表へ
  const handleSelectRace = async (raceId: string) => {
    if (loading) return
    setLoading(true)
    setError('')
    setView('score')
    try {
      const res = await fetch(`${API_BASE}/api/v1/score/${encodeURIComponent(raceId)}`)
      if (!res.ok) {
        if (res.status === 404) throw new Error('該当レースが見つかりません')
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

  return (
    <main className="min-h-screen bg-gray-950 text-gray-200 p-6 font-sans">
      <div className="max-w-[1400px] mx-auto">
        <header className="mb-6">
          <h1 className="text-3xl font-extrabold tracking-tight text-white mb-1">
            Bloodline <span className="text-blue-500">Alpha</span>
          </h1>
          <p className="text-gray-400 text-sm">JRA 新馬戦 血統期待値ダッシュボード</p>
        </header>

        {/* パンくず（開催日 › レース › 出馬表） */}
        {view !== 'dates' && (
          <nav className="mb-4 flex items-center gap-2 text-sm text-gray-400">
            <button onClick={() => setView('dates')} className="hover:text-white transition-colors">開催日</button>
            <span className="text-gray-600">›</span>
            {view === 'races' ? (
              <span className="text-gray-200">{selDisplay}</span>
            ) : (
              <>
                <button onClick={() => setView('races')} className="hover:text-white transition-colors">{selDisplay}</button>
                <span className="text-gray-600">›</span>
                <span className="text-gray-200">出馬表</span>
              </>
            )}
          </nav>
        )}

        {view === 'dates' && <CalendarDates onSelect={handleSelectDate} />}

        {view === 'races' && (
          <CalendarRaces
            date={selDate}
            display={selDisplay}
            onSelect={handleSelectRace}
            onBack={() => setView('dates')}
          />
        )}

        {view === 'score' && (
          <>
            {/* 戻る操作（SPAのためブラウザ戻るは効かない。明示ボタンで遷移） */}
            <div className="mb-4 flex items-center gap-2">
              <button
                onClick={() => setView('dates')}
                className="text-gray-400 hover:text-white text-sm bg-gray-800 hover:bg-gray-700 px-3 py-1.5 rounded-md transition-colors"
              >
                « 開催日選択へ
              </button>
              <button
                onClick={() => setView('races')}
                className="text-gray-400 hover:text-white text-sm bg-gray-800 hover:bg-gray-700 px-3 py-1.5 rounded-md transition-colors"
              >
                ‹ レース選択へ
              </button>
            </div>
            {error && (
              <div className="bg-red-500/10 border-l-4 border-red-500 text-red-400 px-4 py-3 rounded mb-6 text-sm">
                {error}
              </div>
            )}
            {loading && <div className="text-center text-gray-500 text-sm py-8">分析中...</div>}
            {!loading && data && <ScoreTable data={data} updatedAt={updatedAt} />}
          </>
        )}
      </div>
    </main>
  )
}

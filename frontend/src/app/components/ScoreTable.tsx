"use client"

import { RaceData, CATEGORY_COLS, WAKU_COLORS } from '../lib'

// 出馬表＋スコア一覧（TOP画面の表本体）
export default function ScoreTable({ data, updatedAt }: { data: RaceData; updatedAt: string }) {
  return (
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
                  <div className="text-xs text-gray-500 mt-0.5">{p.popularity > 0 ? `${p.popularity}番人気` : '-'}</div>
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
  )
}

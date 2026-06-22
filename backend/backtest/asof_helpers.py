"""as-of 集計の共通部品（Phase 1-B2 / #B1）。

年次チェックポイント方式の point-in-time 集計エンジンと、ライブのスコアリング
サービスと一致させるための条件値変換ヘルパーを提供する。

ライブ側のパーセンタイル算出（backend/app/services/*_score.py）と同一の式・
同一の最低出走数しきい値・同一の条件区分を再現することで、as-of キャッシュが
ライブスコアと同じ意味を持つようにする（唯一の違いは「対象年より前のデータのみ」）。
"""

from __future__ import annotations

import bisect
from collections import defaultdict

# --- 最低出走数しきい値（ライブの各 *_score.py と一致させること） ---
# sire/bms: bloodline_score._build_percentile_cache (starts>=10)
# nicks:    bloodline_score._build_nicks_cache       (starts>=3)
# condition:race_condition_score._MIN_STARTS         (starts>=5)
# human:    human_factor_score._MIN_STARTS           (starts>=10)
# draw/wt:  condition_score._MIN_STARTS              (starts>=20)
THRESHOLDS = {
    "sire": 10,
    "nicks": 3,
    "condition": 5,
    "human": 10,
    "draw": 20,
    "weight": 20,
}

_BABA_MAP = {"1": "good", "2": "yielding", "3": "soft", "4": "heavy"}


def percentile_rank(sorted_values: list[float], value: float) -> float:
    """ソート済みリスト中での百分位（0.0〜1.0）。ライブ実装と同一。"""
    if not sorted_values:
        return 0.0
    idx = bisect.bisect_right(sorted_values, value)
    return idx / len(sorted_values)


def track_to_surface(track_code: str | None) -> str | None:
    """track_code 先頭1桁 → 'turf'(芝) / 'dirt'(ダート) / None(障害等)。"""
    if not track_code:
        return None
    first = track_code[0]
    if first == "1":
        return "turf"
    if first == "2":
        return "dirt"
    return None


def kyori_to_distance_band(kyori: str | None) -> str | None:
    """距離(m) → 'sprint'/'mile'/'middle'/'long'/None。カテゴリB/Eと同区分。"""
    try:
        d = int(kyori)
    except (ValueError, TypeError):
        return None
    if d <= 1400:
        return "sprint"
    if d <= 1800:
        return "mile"
    if d <= 2200:
        return "middle"
    return "long"


def resolve_going(
    surface: str | None,
    shiba_baba_jotai_code: str | None,
    dirt_baba_jotai_code: str | None,
) -> str | None:
    """馬場状態を解決。未設定('0'/None/空)は None（B4対象外）。ライブと同一。"""
    if not surface:
        return None
    if surface == "turf":
        label = _BABA_MAP.get(shiba_baba_jotai_code)
        return f"turf_{label}" if label else None
    label = _BABA_MAP.get(dirt_baba_jotai_code)
    return f"dirt_{label}" if label else None


def roi_numerator(won: bool, tansho_odds: str | None) -> float:
    """単勝回収率の分子寄与。勝利かつオッズ有効時のみ オッズ整数×10。ライブと同一。"""
    if not won:
        return 0.0
    try:
        odds_int = int(tansho_odds)
    except (ValueError, TypeError):
        return 0.0
    return odds_int * 10.0 if odds_int > 0 else 0.0


class AsOfStats:
    """年次チェックポイント方式の as-of 集計エンジン。

    increments を (group_key, item_key, year) 単位で受け取り、advance_to(Y) で
    『Y より前の年』までの累積を構築、snapshot() で母集団のソート済み列＋ルックアップ
    を返す。年は必ず昇順で advance_to すること（内部で取り込み位置を進めるため）。

    - group_key: パーセンタイルを取る母集団の単位（例: ('sire',) や (role,ctype,cvalue)）
    - item_key:  母集団内のルックアップキー（例: 繁殖登録番号、枠番）
    """

    def __init__(self, threshold: int):
        """threshold: 母集団に含める最低出走数（ライブの _MIN_STARTS と一致させる）。"""
        self.threshold = threshold
        # year -> {(group_key, item_key): [starts, wins, roinum]}
        self._by_year: dict[int, dict] = defaultdict(
            lambda: defaultdict(lambda: [0, 0, 0.0])
        )
        # 累積（advance_to で更新）
        self._running: dict = defaultdict(lambda: [0, 0, 0.0])
        self._sorted_years: list[int] | None = None
        self._next_idx = 0

    def add(self, group_key, item_key, year: int, won: bool, roinum: float) -> None:
        """1走分の出走実績を (group_key, item_key, year) の増分として登録する。"""
        cell = self._by_year[year][(group_key, item_key)]
        cell[0] += 1
        if won:
            cell[1] += 1
        cell[2] += roinum

    def advance_to(self, target_year: int) -> None:
        """target_year より前(<)の全年を running に取り込む。昇順で呼ぶこと。"""
        if self._sorted_years is None:
            self._sorted_years = sorted(self._by_year.keys())
        years = self._sorted_years
        while self._next_idx < len(years) and years[self._next_idx] < target_year:
            for gi, vals in self._by_year[years[self._next_idx]].items():
                r = self._running[gi]
                r[0] += vals[0]
                r[1] += vals[1]
                r[2] += vals[2]
            self._next_idx += 1

    def snapshot(self) -> dict:
        """現在の累積から group_key ごとの母集団(ソート済 wr/roi)とルックアップを構築。

        返却: {group_key: {"lookup": {item_key: (wr, roi)}, "wr": [...], "roi": [...]}}
        starts がしきい値未満の item は母集団・ルックアップとも除外（ライブと同一）。
        """
        tmp: dict = defaultdict(lambda: {"lookup": {}, "wr": [], "roi": []})
        for (gk, ik), (starts, wins, roinum) in self._running.items():
            if starts < self.threshold:
                continue
            wr = wins / starts
            roi = roinum / (starts * 100.0)
            g = tmp[gk]
            g["lookup"][ik] = (wr, roi)
            g["wr"].append(wr)
            g["roi"].append(roi)
        for g in tmp.values():
            g["wr"].sort()
            g["roi"].sort()
        return dict(tmp)


def feature(snapshot: dict, group_key, item_key) -> tuple[float | None, float | None]:
    """snapshot から (wr_pctl, roi_pctl) を取得。データ無しは (None, None)。"""
    g = snapshot.get(group_key)
    if not g:
        return (None, None)
    v = g["lookup"].get(item_key)
    if not v:
        return (None, None)
    return (percentile_rank(g["wr"], v[0]), percentile_rank(g["roi"], v[1]))

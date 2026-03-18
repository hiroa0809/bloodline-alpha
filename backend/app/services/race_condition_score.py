"""
カテゴリB（レース条件）スコア計算

sire_condition_stats テーブルの条件別成績をパーセンタイル順位でスコア化する。
B1: 馬場（芝/ダート）、B2: 距離帯、B3: 開催地、B4: 馬場状態
"""

import asyncio
import bisect
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 配点デフォルト値（将来的にAPIパラメータで可変化）
DEFAULT_WEIGHTS = {
    "B1": 5,   # 馬場（芝/ダート）適性
    "B2": 5,   # 距離帯適性
    "B3": 4,   # 開催地適性
    "B4": 3,   # 馬場状態適性
}

# 条件別キャッシュ（サーバー起動中は保持）
_condition_cache: dict = {}
_condition_cache_lock = asyncio.Lock()

# 条件別は全体よりサンプルが少ないため閾値を下げる
_MIN_STARTS = 5


# --- ユーティリティ ---

def _percentile_rank(sorted_values: list[float], value: float) -> float:
    """ソート済みリスト中での百分位（0.0〜1.0）を返す"""
    if not sorted_values:
        return 0.0
    idx = bisect.bisect_right(sorted_values, value)
    return idx / len(sorted_values)


# --- 入力値変換ヘルパー ---

def _track_to_surface(track_code: str) -> str | None:
    """track_code先頭1桁 → 'turf' / 'dirt' / None(障害等)"""
    if not track_code:
        return None
    first = track_code[0]
    if first == "1":
        return "turf"
    if first == "2":
        return "dirt"
    return None


def _kyori_to_distance_band(kyori: str) -> str | None:
    """距離(m) → 'sprint' / 'mile' / 'middle' / 'long' / None(変換不能)"""
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


def _resolve_going(
    track_code: str,
    shiba_baba_jotai_code: str | None,
    dirt_baba_jotai_code: str | None,
) -> str | None:
    """馬場状態コードを解決。未設定('0'/None/空)はNoneを返す"""
    surface = _track_to_surface(track_code)
    if not surface:
        return None

    _BABA_MAP = {"1": "good", "2": "yielding", "3": "soft", "4": "heavy"}

    if surface == "turf":
        label = _BABA_MAP.get(shiba_baba_jotai_code)
        return f"turf_{label}" if label else None
    else:
        label = _BABA_MAP.get(dirt_baba_jotai_code)
        return f"dirt_{label}" if label else None


# --- キャッシュ構築 ---

async def _build_condition_cache(db: AsyncSession) -> dict:
    """sire_condition_stats から条件別のパーセンタイルキャッシュを構築"""
    new_cache = {}

    result = await db.execute(
        text(
            "SELECT hanshoku_bango, role, condition_type, condition_value, "
            "  bamei, win_rate, tansho_roi "
            "FROM sire_condition_stats "
            "WHERE starts >= :min_starts "
            "ORDER BY hanshoku_bango"
        ),
        {"min_starts": _MIN_STARTS},
    )
    rows = result.fetchall()

    # (role, condition_type, condition_value) ごとにグルーピング
    # grouped[role][condition_type][condition_value] = [row_data, ...]
    grouped: dict[str, dict[str, dict[str, list]]] = {}
    for row in rows:
        bango, role, ctype, cvalue, bamei, win_rate, roi = row
        grouped.setdefault(role, {}).setdefault(ctype, {}).setdefault(cvalue, []).append(
            (bango, bamei, win_rate, roi)
        )

    # キャッシュ構造を構築
    for role, ctypes in grouped.items():
        for ctype, cvalues in ctypes.items():
            cache_key = (role, ctype)
            win_rates_by_cv = {}
            rois_by_cv = {}
            lookup_by_cv = {}

            for cvalue, entries in cvalues.items():
                win_rates_by_cv[cvalue] = sorted(e[2] for e in entries)
                rois_by_cv[cvalue] = sorted(e[3] for e in entries)
                lookup_by_cv[cvalue] = {
                    e[0]: {"bamei": e[1], "win_rate": e[2], "roi": e[3]}
                    for e in entries
                }

            new_cache[cache_key] = {
                "win_rates": win_rates_by_cv,
                "rois": rois_by_cv,
                "lookup": lookup_by_cv,
            }

    total_entries = len(rows)
    logger.info(f"条件別キャッシュ構築完了: {total_entries:,} エントリ")
    return new_cache


async def ensure_condition_cache(db: AsyncSession) -> None:
    """キャッシュが空なら構築する。呼び出し元でループ前に1回呼ぶ。"""
    global _condition_cache
    if _condition_cache:
        return
    async with _condition_cache_lock:
        if not _condition_cache:
            _condition_cache = await _build_condition_cache(db)


async def refresh_condition_cache(db: AsyncSession) -> None:
    """キャッシュを強制再構築する（バッチ実行後に呼ぶ）"""
    global _condition_cache
    async with _condition_cache_lock:
        _condition_cache = await _build_condition_cache(db)


# --- スコア計算 ---

def _calc_condition_sub_score(
    condition_type: str,
    condition_value: str | None,
    sire_bango: str | None,
    bms_bango: str | None,
    weight: float,
) -> float:
    """
    1サブスコアを計算。
    父のパーセンタイル(60%) + BMSのパーセンタイル(40%) の加重平均 × 配点
    """
    if not condition_value:
        return 0.0

    sire_pctl = _get_percentile("sire", condition_type, condition_value, sire_bango)
    bms_pctl = _get_percentile("bms", condition_type, condition_value, bms_bango)

    # 片方しかデータがない場合はそちらのみで計算
    if sire_pctl is not None and bms_pctl is not None:
        combined = sire_pctl * 0.6 + bms_pctl * 0.4
    elif sire_pctl is not None:
        combined = sire_pctl
    elif bms_pctl is not None:
        combined = bms_pctl
    else:
        return 0.0

    return round(combined * weight, 1)


def _get_percentile(
    role: str,
    condition_type: str,
    condition_value: str,
    hanshoku_bango: str | None,
) -> float | None:
    """指定条件でのパーセンタイルを取得。データなしはNone。"""
    if not hanshoku_bango:
        return None

    cache = _condition_cache.get((role, condition_type))
    if not cache:
        return None

    lookup = cache["lookup"].get(condition_value)
    if not lookup:
        return None

    info = lookup.get(hanshoku_bango)
    if not info:
        return None

    win_rates = cache["win_rates"].get(condition_value, [])
    rois = cache["rois"].get(condition_value, [])

    wr_pctl = _percentile_rank(win_rates, info["win_rate"])
    roi_pctl = _percentile_rank(rois, info["roi"])

    # 勝率60% + ROI40%
    return wr_pctl * 0.6 + roi_pctl * 0.4


def calc_race_condition_score(
    sire_bango: str | None,
    bms_bango: str | None,
    track_code: str,
    kyori: str,
    keibajo_code: str,
    shiba_baba_jotai_code: str | None,
    dirt_baba_jotai_code: str | None,
) -> dict:
    """
    1頭分のカテゴリBスコアを計算して返す。
    DBアクセスなし — キャッシュのみで計算。
    事前に ensure_condition_cache() を呼んでおくこと。

    返却例:
    {"B1": 4.2, "B2": 3.8, "B3": 2.1, "B4": 1.5, "total": 11.6}
    """
    if not _condition_cache:
        logger.warning("条件別キャッシュが未初期化です。ensure_condition_cache()を呼んでください。")

    # 入力値を条件値に変換
    surface = _track_to_surface(track_code)
    distance_band = _kyori_to_distance_band(kyori)
    going = _resolve_going(track_code, shiba_baba_jotai_code, dirt_baba_jotai_code)

    # B1: 馬場適性
    b1 = _calc_condition_sub_score("surface", surface, sire_bango, bms_bango, DEFAULT_WEIGHTS["B1"])
    # B2: 距離帯適性
    b2 = _calc_condition_sub_score("distance", distance_band, sire_bango, bms_bango, DEFAULT_WEIGHTS["B2"])
    # B3: 開催地適性
    b3 = _calc_condition_sub_score("venue", keibajo_code, sire_bango, bms_bango, DEFAULT_WEIGHTS["B3"])
    # B4: 馬場状態適性
    b4 = _calc_condition_sub_score("going", going, sire_bango, bms_bango, DEFAULT_WEIGHTS["B4"])

    total = round(b1 + b2 + b3 + b4, 1)

    return {
        "B1": b1,
        "B2": b2,
        "B3": b3,
        "B4": b4,
        "total": total,
    }

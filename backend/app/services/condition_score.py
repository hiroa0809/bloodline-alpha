"""
カテゴリE（コンディション）スコア計算

draw_stats / weight_stats（新馬戦701の集計）をパーセンタイル順位でスコア化する。
E1: 枠順（競馬場×距離帯ごとの枠番ベース成績）
E2: 斤量（負担重量ごとのベース成績）

新馬戦は馬自身の過去走が無いため、馬の属性ではなく枠・斤量という条件側の
ベース成績をスコアに使う。
"""

import asyncio
import bisect
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 配点デフォルト値（将来的にAPIパラメータで可変化・最終重みはIS校正で決定）
DEFAULT_WEIGHTS = {
    "E1": 3,   # 枠順
    "E2": 2,   # 斤量
}

# キャッシュ（サーバー起動中は保持）
# _draw_cache:   {(keibajo_code, distance_band): {"win_rates":[...], "rois":[...], "lookup": {wakuban: {...}}}}
# _weight_cache: {"win_rates":[...], "rois":[...], "lookup": {futan_juryo: {...}}}
_draw_cache: dict = {}
_weight_cache: dict = {}
_condition_score_cache_initialized = False
_condition_score_cache_lock = asyncio.Lock()

# 最低出走数閾値（これ未満のセルはスコア対象外）
_MIN_STARTS = 20


# --- ユーティリティ ---

def _percentile_rank(sorted_values: list[float], value: float) -> float:
    """ソート済みリスト中での百分位（0.0〜1.0）を返す"""
    if not sorted_values:
        return 0.0
    idx = bisect.bisect_right(sorted_values, value)
    return idx / len(sorted_values)


def _kyori_to_distance_band(kyori: str) -> str | None:
    """距離(m) → 'sprint' / 'mile' / 'middle' / 'long' / None(変換不能)。カテゴリBと同区分。"""
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


def _track_to_surface(track_code: str) -> str | None:
    """track_code先頭1桁 → 'turf'(芝) / 'dirt'(ダート) / None(障害等)。カテゴリBと同区分。"""
    if not track_code:
        return None
    first = track_code[0]
    if first == "1":
        return "turf"
    if first == "2":
        return "dirt"
    return None


# --- キャッシュ構築 ---

async def _build_condition_score_cache(db: AsyncSession) -> tuple[dict, dict] | None:
    """draw_stats / weight_stats からパーセンタイルキャッシュを構築。
    テーブル未作成時は None を返す（カテゴリEのみ無効化）。
    """
    check = await db.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('draw_stats', 'weight_stats')"
        )
    )
    names = {row[0] for row in check.fetchall()}
    if "draw_stats" not in names or "weight_stats" not in names:
        logger.warning(
            "draw_stats / weight_stats テーブルが存在しません。"
            "カテゴリEは0点で計算されます。"
            "python backend/batch/calc_condition_stats.py を実行してください。"
        )
        return None

    # E1: draw_stats を (keibajo, surface, distance_band) でグルーピング
    draw_cache: dict = {}
    result = await db.execute(
        text(
            "SELECT keibajo_code, surface, distance_band, wakuban, win_rate, tansho_roi "
            "FROM draw_stats WHERE starts >= :min_starts"
        ),
        {"min_starts": _MIN_STARTS},
    )
    grouped: dict = {}
    for keibajo, surface, dband, wakuban, win_rate, roi in result.fetchall():
        grouped.setdefault((keibajo, surface, dband), []).append((wakuban, win_rate, roi))
    for key, entries in grouped.items():
        draw_cache[key] = {
            "win_rates": sorted(e[1] for e in entries),
            "rois": sorted(e[2] for e in entries),
            "lookup": {e[0]: {"win_rate": e[1], "roi": e[2]} for e in entries},
        }

    # E2: weight_stats を全体で1グループ
    result = await db.execute(
        text(
            "SELECT futan_juryo, win_rate, tansho_roi "
            "FROM weight_stats WHERE starts >= :min_starts"
        ),
        {"min_starts": _MIN_STARTS},
    )
    wrows = result.fetchall()
    weight_cache = {
        "win_rates": sorted(r[1] for r in wrows),
        "rois": sorted(r[2] for r in wrows),
        "lookup": {r[0]: {"win_rate": r[1], "roi": r[2]} for r in wrows},
    }

    logger.info(
        f"コンディションキャッシュ構築完了: "
        f"draw={len(draw_cache)} 群, weight={len(weight_cache['lookup'])} 種別"
    )
    return draw_cache, weight_cache


async def ensure_condition_score_cache(db: AsyncSession) -> None:
    """キャッシュが空なら構築する。呼び出し元でループ前に1回呼ぶ。
    テーブル未作成時はキャッシュを空のままにし、カテゴリEは0点で計算される。
    """
    global _draw_cache, _weight_cache, _condition_score_cache_initialized
    if _condition_score_cache_initialized:
        return
    async with _condition_score_cache_lock:
        if not _condition_score_cache_initialized:
            result = await _build_condition_score_cache(db)
            if result is not None:
                _draw_cache, _weight_cache = result
            else:
                _draw_cache, _weight_cache = {}, {}
            _condition_score_cache_initialized = True


async def refresh_condition_score_cache(db: AsyncSession) -> None:
    """キャッシュを強制再構築する（バッチ実行後に呼ぶ）"""
    global _draw_cache, _weight_cache, _condition_score_cache_initialized
    async with _condition_score_cache_lock:
        result = await _build_condition_score_cache(db)
        if result is not None:
            _draw_cache, _weight_cache = result
        else:
            _draw_cache, _weight_cache = {}, {}
        _condition_score_cache_initialized = True


# --- スコア計算 ---

def _calc_e1(
    keibajo_code: str | None, track_code: str, kyori: str, wakuban: str | None, weight: float
) -> float:
    """E1: 枠順スコア。競馬場×馬場(芝/ダート)×距離帯ごとの枠番ベース成績をパーセンタイル化。"""
    if not wakuban or not keibajo_code:
        return 0.0
    surface = _track_to_surface(track_code)
    distance_band = _kyori_to_distance_band(kyori)
    if not surface or not distance_band:
        return 0.0
    cache = _draw_cache.get((keibajo_code, surface, distance_band))
    if not cache:
        return 0.0
    info = cache["lookup"].get(str(wakuban).strip())
    if not info:
        return 0.0
    wr_pctl = _percentile_rank(cache["win_rates"], info["win_rate"])
    roi_pctl = _percentile_rank(cache["rois"], info["roi"])
    # 勝率60% + ROI40%
    combined = wr_pctl * 0.6 + roi_pctl * 0.4
    return round(combined * weight, 1)


def _calc_e2(futan_juryo: str | None, weight: float) -> float:
    """E2: 斤量スコア。斤量別ベース成績をパーセンタイル化。"""
    if not futan_juryo or not _weight_cache:
        return 0.0
    info = _weight_cache["lookup"].get(str(futan_juryo).strip())
    if not info:
        return 0.0
    wr_pctl = _percentile_rank(_weight_cache["win_rates"], info["win_rate"])
    roi_pctl = _percentile_rank(_weight_cache["rois"], info["roi"])
    # 勝率60% + ROI40%
    combined = wr_pctl * 0.6 + roi_pctl * 0.4
    return round(combined * weight, 1)


def calc_condition_score(
    keibajo_code: str | None,
    track_code: str,
    kyori: str,
    wakuban: str | None,
    futan_juryo: str | None,
    weights: dict[str, float] | None = None,
) -> dict:
    """
    1頭分のカテゴリEスコアを計算して返す。
    DBアクセスなし — キャッシュのみで計算。
    事前に ensure_condition_score_cache() を呼んでおくこと。

    weights: {"E1": float, "E2": float} — 省略時はDEFAULT_WEIGHTSを使用。

    返却例: {"E1": 2.1, "E2": 1.2, "total": 3.3}
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    if not _draw_cache and not _weight_cache:
        logger.warning(
            "コンディションキャッシュが未初期化です。ensure_condition_score_cache()を呼んでください。"
        )

    e1 = _calc_e1(keibajo_code, track_code, kyori, wakuban, w["E1"])
    e2 = _calc_e2(futan_juryo, w["E2"])
    total = round(e1 + e2, 1)

    return {
        "E1": e1,
        "E2": e2,
        "total": total,
    }

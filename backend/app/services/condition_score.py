"""
カテゴリE（コンディション）スコア計算

weight_stats（新馬戦701の集計）をパーセンタイル順位でスコア化する。
E2: 斤量（負担重量ごとのベース成績）

新馬戦は馬自身の過去走が無いため、馬の属性ではなく斤量という条件側の
ベース成績をスコアに使う。

※ E1(枠順) は #B5 Phase1 信号診断で予測力なし（レース内AUC≈0.50・市場超え増分も無し）
  と判明したため 2026-06-24 に除外（draw_stats 由来の枠順スコア機構を撤去）。
"""

import asyncio
import bisect
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 配点デフォルト値（将来的にAPIパラメータで可変化・最終重みはIS校正で決定）
DEFAULT_WEIGHTS = {
    "E2": 2,  # 斤量
}

# キャッシュ（サーバー起動中は保持）
# _weight_cache: {"win_rates":[...], "rois":[...], "lookup": {futan_juryo: {...}}}
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


# --- キャッシュ構築 ---


async def _build_condition_score_cache(db: AsyncSession) -> dict | None:
    """weight_stats からパーセンタイルキャッシュを構築。
    テーブル未作成時は None を返す（カテゴリEのみ無効化）。
    """
    check = await db.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = 'weight_stats'"
        )
    )
    if not check.fetchone():
        logger.warning(
            "weight_stats テーブルが存在しません。カテゴリEは0点で計算されます。"
            "python backend/batch/calc_condition_stats.py を実行してください。"
        )
        return None

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
        f"コンディションキャッシュ構築完了: weight={len(weight_cache['lookup'])} 種別"
    )
    return weight_cache


async def ensure_condition_score_cache(db: AsyncSession) -> None:
    """キャッシュが空なら構築する。呼び出し元でループ前に1回呼ぶ。
    テーブル未作成時はキャッシュを空のままにし、カテゴリEは0点で計算される。
    """
    global _weight_cache, _condition_score_cache_initialized
    if _condition_score_cache_initialized:
        return
    async with _condition_score_cache_lock:
        if not _condition_score_cache_initialized:
            result = await _build_condition_score_cache(db)
            _weight_cache = result if result is not None else {}
            _condition_score_cache_initialized = True


async def refresh_condition_score_cache(db: AsyncSession) -> None:
    """キャッシュを強制再構築する（バッチ実行後に呼ぶ）"""
    global _weight_cache, _condition_score_cache_initialized
    async with _condition_score_cache_lock:
        result = await _build_condition_score_cache(db)
        _weight_cache = result if result is not None else {}
        _condition_score_cache_initialized = True


# --- スコア計算 ---


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
    futan_juryo: str | None,
    weights: dict[str, float] | None = None,
) -> dict:
    """
    1頭分のカテゴリEスコア（E2斤量のみ）を計算して返す。
    DBアクセスなし — キャッシュのみで計算。
    事前に ensure_condition_score_cache() を呼んでおくこと。

    weights: {"E2": float} — 省略時はDEFAULT_WEIGHTSを使用。

    返却例: {"E2": 1.2, "total": 1.2}
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    if not _weight_cache:
        logger.warning(
            "コンディションキャッシュが未初期化です。ensure_condition_score_cache()を呼んでください。"
        )

    e2 = _calc_e2(futan_juryo, w["E2"])
    return {"E2": e2, "total": e2}

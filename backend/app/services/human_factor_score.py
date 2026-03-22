"""
カテゴリC（人的要素）スコア計算

human_factor_stats テーブルの成績をパーセンタイル順位でスコア化する。
C1: 調教師、C2: 騎手、C3: 馬主/生産者
"""

import asyncio
import bisect
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 配点デフォルト値（将来的にAPIパラメータで可変化）
DEFAULT_WEIGHTS = {
    "C1": 4,   # 調教師
    "C2": 4,   # 騎手
    "C3": 2,   # 馬主/生産者
}

# キャッシュ（サーバー起動中は保持）
# 構造: {role: {"win_rates": [...], "rois": [...], "lookup": {code: {name, win_rate, roi}}}}
_human_factor_cache: dict = {}
_human_factor_cache_initialized = False
_human_factor_cache_lock = asyncio.Lock()

# 最低出走数閾値（これ未満はスコア対象外）
_MIN_STARTS = 10


# --- ユーティリティ ---

def _percentile_rank(sorted_values: list[float], value: float) -> float:
    """ソート済みリスト中での百分位（0.0〜1.0）を返す"""
    if not sorted_values:
        return 0.0
    idx = bisect.bisect_right(sorted_values, value)
    return idx / len(sorted_values)


# --- キャッシュ構築 ---

async def _build_human_factor_cache(db: AsyncSession) -> dict | None:
    """human_factor_stats から role 別にパーセンタイルキャッシュを構築。
    テーブル未作成時は None を返す（カテゴリCのみ無効化）。
    """
    # テーブル存在チェック
    check = await db.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='human_factor_stats'")
    )
    if not check.fetchone():
        logger.warning(
            "human_factor_stats テーブルが存在しません。"
            "カテゴリCは0点で計算されます。"
            "python backend/batch/calc_human_factor_stats.py を実行してください。"
        )
        return None

    new_cache = {}

    for role in ("jockey", "trainer", "owner", "breeder"):
        result = await db.execute(
            text(
                "SELECT person_code, person_name, win_rate, tansho_roi "
                "FROM human_factor_stats WHERE role = :role AND starts >= :min_starts "
                "ORDER BY person_code"
            ),
            {"role": role, "min_starts": _MIN_STARTS},
        )
        rows = result.fetchall()

        # ソート済みリスト（パーセンタイル計算用）
        win_rates = sorted(r[2] for r in rows)
        rois = sorted(r[3] for r in rows)

        # person_code → {name, win_rate, roi} のルックアップ
        lookup = {
            r[0]: {"name": r[1], "win_rate": r[2], "roi": r[3]}
            for r in rows
        }

        new_cache[role] = {
            "win_rates": win_rates,
            "rois": rois,
            "lookup": lookup,
        }

    logger.info(
        f"人的要素キャッシュ構築完了: "
        f"jockey={len(new_cache['jockey']['lookup'])}名, "
        f"trainer={len(new_cache['trainer']['lookup'])}名, "
        f"owner={len(new_cache['owner']['lookup'])}名/社, "
        f"breeder={len(new_cache['breeder']['lookup'])}社"
    )
    return new_cache


async def ensure_human_factor_cache(db: AsyncSession) -> None:
    """キャッシュが空なら構築する。呼び出し元でループ前に1回呼ぶ。
    テーブル未作成時はキャッシュを空dictのままにし、カテゴリCは0点で計算される。
    """
    global _human_factor_cache, _human_factor_cache_initialized
    if _human_factor_cache_initialized:
        return
    async with _human_factor_cache_lock:
        if not _human_factor_cache_initialized:
            result = await _build_human_factor_cache(db)
            _human_factor_cache = result if result is not None else {}
            _human_factor_cache_initialized = True


async def refresh_human_factor_cache(db: AsyncSession) -> None:
    """キャッシュを強制再構築する（バッチ実行後に呼ぶ）"""
    global _human_factor_cache, _human_factor_cache_initialized
    async with _human_factor_cache_lock:
        result = await _build_human_factor_cache(db)
        _human_factor_cache = result if result is not None else {}
        _human_factor_cache_initialized = True


# --- スコア計算 ---

def _calc_person_score(role: str, person_code: str | None, weight: float) -> float:
    """
    1人物のパーセンタイルスコアを計算。
    勝率60% + ROI40% の加重平均 × 配点
    """
    if not person_code or not _human_factor_cache:
        return 0.0

    cache = _human_factor_cache.get(role)
    if not cache:
        return 0.0

    info = cache["lookup"].get(person_code)
    if not info:
        return 0.0

    wr_pctl = _percentile_rank(cache["win_rates"], info["win_rate"])
    roi_pctl = _percentile_rank(cache["rois"], info["roi"])

    # 加重平均: 勝率60% + ROI40%
    combined = wr_pctl * 0.6 + roi_pctl * 0.4
    return round(combined * weight, 1)


def calc_human_factor_score(
    chokyoshi_code: str | None,
    kishu_code: str | None,
    banushi_code: str | None,
    seisansha_code: str | None,
    weights: dict[str, float] | None = None,
) -> dict:
    """
    1頭分のカテゴリCスコアを計算して返す。
    DBアクセスなし — キャッシュのみで計算。
    事前に ensure_human_factor_cache() を呼んでおくこと。

    weights: {"C1": float, "C2": float, "C3": float} — 省略時はDEFAULT_WEIGHTSを使用。
             新馬戦/未勝利以上でレースプロファイルに応じた配点を渡す。

    返却例:
    {"C1": 3.2, "C2": 3.8, "C3": 1.5, "total": 8.5}
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    if not _human_factor_cache:
        logger.warning("人的要素キャッシュが未初期化です。ensure_human_factor_cache()を呼んでください。")

    # C1: 調教師
    c1 = _calc_person_score("trainer", chokyoshi_code, w["C1"])

    # C2: 騎手
    c2 = _calc_person_score("jockey", kishu_code, w["C2"])

    # C3: 馬主/生産者（50:50 で合算）
    # 配点の半分ずつを馬主・生産者に割り当て
    c3_weight_each = w["C3"] / 2.0
    c3_owner = _calc_person_score("owner", banushi_code, c3_weight_each)
    c3_breeder = _calc_person_score("breeder", seisansha_code, c3_weight_each)
    c3 = round(c3_owner + c3_breeder, 1)

    total = round(c1 + c2 + c3, 1)

    return {
        "C1": c1,
        "C2": c2,
        "C3": c3,
        "total": total,
    }

"""
カテゴリA（血統）スコア計算

sire_stats テーブルの種牡馬・BMS成績をパーセンタイル順位でスコア化する。
A1: 父成績、A2: BMS成績、A3〜A5: 未実装（0を返す）
"""

import ast
import bisect
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 配点デフォルト値（将来的にAPIパラメータで可変化）
DEFAULT_WEIGHTS = {
    "A1": 28,   # 父成績
    "A2": 16,   # BMS成績
    "A3": 8,    # ニックス（未実装）
    "A4": 7,    # インブリード（未実装）
    "A5": 6,    # アウトブリード（未実装）
}

# パーセンタイル算出用キャッシュ（サーバー起動中は保持）
_percentile_cache: dict = {}


def _percentile_rank(sorted_values: list[float], value: float) -> float:
    """ソート済みリスト中での百分位（0.0〜1.0）を返す"""
    if not sorted_values:
        return 0.0
    idx = bisect.bisect_left(sorted_values, value)
    return idx / len(sorted_values)


async def _build_percentile_cache(db: AsyncSession) -> None:
    """sire_stats から role 別にソート済みリストを構築してキャッシュ"""
    for role in ("sire", "bms"):
        result = await db.execute(
            text(
                "SELECT hanshoku_bango, bamei, win_rate, tansho_roi "
                "FROM sire_stats WHERE role = :role AND starts >= 10 "
                "ORDER BY hanshoku_bango"
            ),
            {"role": role},
        )
        rows = result.fetchall()

        # ソート済みリスト（パーセンタイル計算用）
        win_rates = sorted(r[2] for r in rows)
        rois = sorted(r[3] for r in rows)

        # hanshoku_bango → (bamei, win_rate, roi) のルックアップ
        lookup = {r[0]: {"bamei": r[1], "win_rate": r[2], "roi": r[3]} for r in rows}

        _percentile_cache[role] = {
            "win_rates": win_rates,
            "rois": rois,
            "lookup": lookup,
        }

    logger.info(
        f"パーセンタイルキャッシュ構築完了: "
        f"sire={len(_percentile_cache['sire']['lookup'])}頭, "
        f"bms={len(_percentile_cache['bms']['lookup'])}頭"
    )


def _calc_sub_score(role: str, hanshoku_bango: str, weight: float) -> tuple[float, dict | None]:
    """
    1種牡馬/BMSのサブスコアを計算。
    返却: (スコア, info_dict or None)
    """
    cache = _percentile_cache.get(role)
    if not cache or not hanshoku_bango:
        return 0.0, None

    info = cache["lookup"].get(hanshoku_bango)
    if not info:
        return 0.0, None

    # パーセンタイル算出
    wr_pctl = _percentile_rank(cache["win_rates"], info["win_rate"])
    roi_pctl = _percentile_rank(cache["rois"], info["roi"])

    # 加重平均: 勝率60% + ROI40%
    combined = wr_pctl * 0.6 + roi_pctl * 0.4
    score = round(combined * weight, 1)

    return score, {
        "name": info["bamei"] or "",
        "hanshoku_bango": hanshoku_bango,
        "win_rate": round(info["win_rate"], 4),
        "roi": round(info["roi"], 4),
    }


async def calc_bloodline_score(
    db: AsyncSession,
    ketto_toroku_bango: str,
) -> dict:
    """
    1頭分のカテゴリAスコアを計算して返す。

    返却例:
    {
        "A1": 25.2, "A2": 14.5, "A3": 0, "A4": 0, "A5": 0,
        "total": 39.7,
        "sire_info": {...}, "bms_info": {...}
    }
    """
    # キャッシュが空なら構築
    if not _percentile_cache:
        await _build_percentile_cache(db)

    # jvd_uma から sandai_ketto を取得
    result = await db.execute(
        text("SELECT sandai_ketto FROM jvd_uma WHERE ketto_toroku_bango = :bango"),
        {"bango": ketto_toroku_bango},
    )
    row = result.fetchone()

    sire_bango = None
    bms_bango = None

    if row and row[0]:
        try:
            ketto_list = ast.literal_eval(row[0])
            if len(ketto_list) > 0 and isinstance(ketto_list[0], dict):
                sire_bango = ketto_list[0].get("hanshoku_toroku_bango", "").strip() or None
            if len(ketto_list) > 4 and isinstance(ketto_list[4], dict):
                bms_bango = ketto_list[4].get("hanshoku_toroku_bango", "").strip() or None
        except (ValueError, SyntaxError):
            pass

    # A1: 父成績
    a1_score, sire_info = _calc_sub_score("sire", sire_bango, DEFAULT_WEIGHTS["A1"])
    # A2: BMS成績
    a2_score, bms_info = _calc_sub_score("bms", bms_bango, DEFAULT_WEIGHTS["A2"])

    total = round(a1_score + a2_score, 1)

    return {
        "A1": a1_score,
        "A2": a2_score,
        "A3": 0.0,
        "A4": 0.0,
        "A5": 0.0,
        "total": total,
        "sire_info": sire_info,
        "bms_info": bms_info,
    }

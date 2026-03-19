"""
カテゴリA（血統）スコア計算

A1: 父成績、A2: BMS成績、A3: ニックス、A4: インブリード、A5: アウトブリード
"""

import ast
import asyncio
import bisect
import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 配点デフォルト値（将来的にAPIパラメータで可変化）
DEFAULT_WEIGHTS = {
    "A1": 28,   # 父成績
    "A2": 16,   # BMS成績
    "A3": 8,    # ニックス
    "A4": 7,    # インブリード
    "A5": 6,    # アウトブリード
}

# --- sandai_ketto インデックス定義 ---
# 世代マッピング: インデックス → 世代数（馬自身から数えて）
# Gen1(親): 0=父, 1=母  Gen2(祖父母): 2-5  Gen3(曾祖父母): 6-13
_IDX_TO_GEN = {0: 1, 1: 1, 2: 2, 3: 2, 4: 2, 5: 2,
               6: 3, 7: 3, 8: 3, 9: 3, 10: 3, 11: 3, 12: 3, 13: 3}
# 父方の祖先インデックス（Gen2以降。父自身はGen1なので比較対象外）
_SIRE_ANCESTOR_INDICES = (2, 3, 6, 7, 8, 9)
# 母方の祖先インデックス（Gen2以降）
_DAM_ANCESTOR_INDICES = (4, 5, 10, 11, 12, 13)
# COI正規化の上限値（3代血統内の実用的な上限）
_COI_NORMALIZE_MAX = 0.15

# --- パーセンタイル算出用キャッシュ（サーバー起動中は保持、アトミックに差し替え） ---
_percentile_cache: dict = {}
_cache_lock = asyncio.Lock()

# --- ニックスキャッシュ ---
_nicks_cache: dict = {}
_nicks_cache_lock = asyncio.Lock()


def _percentile_rank(sorted_values: list[float], value: float) -> float:
    """ソート済みリスト中での百分位（0.0〜1.0）を返す"""
    if not sorted_values:
        return 0.0
    idx = bisect.bisect_right(sorted_values, value)
    return idx / len(sorted_values)


# ============================================================
# A1/A2: 父・BMS成績キャッシュ
# ============================================================

async def _build_percentile_cache(db: AsyncSession) -> dict:
    """sire_stats から role 別にソート済みリストを構築して新しいキャッシュを返す"""
    new_cache = {}
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

        new_cache[role] = {
            "win_rates": win_rates,
            "rois": rois,
            "lookup": lookup,
        }

    logger.info(
        f"パーセンタイルキャッシュ構築完了: "
        f"sire={len(new_cache['sire']['lookup'])}頭, "
        f"bms={len(new_cache['bms']['lookup'])}頭"
    )
    return new_cache


async def ensure_percentile_cache(db: AsyncSession) -> None:
    """キャッシュが空なら構築する。呼び出し元でループ前に1回呼ぶ。"""
    global _percentile_cache
    if _percentile_cache:
        return
    async with _cache_lock:
        if not _percentile_cache:
            _percentile_cache = await _build_percentile_cache(db)


async def refresh_percentile_cache(db: AsyncSession) -> None:
    """キャッシュを強制再構築する（sire_statsバッチ実行後に呼ぶ）"""
    global _percentile_cache
    async with _cache_lock:
        _percentile_cache = await _build_percentile_cache(db)


# ============================================================
# A3: ニックスキャッシュ
# ============================================================

async def _build_nicks_cache(db: AsyncSession) -> dict:
    """nicks_stats からパーセンタイル用キャッシュを構築"""
    result = await db.execute(
        text(
            "SELECT sire_bango, bms_bango, sire_bamei, bms_bamei, win_rate, tansho_roi "
            "FROM nicks_stats WHERE starts >= 3 "
            "ORDER BY sire_bango, bms_bango"
        )
    )
    rows = result.fetchall()

    win_rates = sorted(r[4] for r in rows)
    rois = sorted(r[5] for r in rows)

    lookup = {}
    for r in rows:
        lookup[(r[0], r[1])] = {
            "sire_bamei": r[2],
            "bms_bamei": r[3],
            "win_rate": r[4],
            "roi": r[5],
        }

    logger.info(f"ニックスキャッシュ構築完了: {len(lookup):,} 組合せ")
    return {
        "win_rates": win_rates,
        "rois": rois,
        "lookup": lookup,
    }


async def ensure_nicks_cache(db: AsyncSession) -> None:
    """ニックスキャッシュが空なら構築する。"""
    global _nicks_cache
    if _nicks_cache:
        return
    async with _nicks_cache_lock:
        if not _nicks_cache:
            _nicks_cache = await _build_nicks_cache(db)


async def refresh_nicks_cache(db: AsyncSession) -> None:
    """ニックスキャッシュを強制再構築する"""
    global _nicks_cache
    async with _nicks_cache_lock:
        _nicks_cache = await _build_nicks_cache(db)


# ============================================================
# サブスコア計算関数
# ============================================================

def _calc_sub_score(role: str, hanshoku_bango: str, weight: float) -> tuple[float, dict | None]:
    """
    A1/A2: 1種牡馬/BMSのサブスコアを計算。
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


def _calc_nicks_score(
    sire_bango: str | None, bms_bango: str | None, weight: float
) -> tuple[float, dict | None]:
    """
    A3: 父×BMS ニックスのサブスコアを計算。
    返却: (スコア, info_dict or None)
    """
    if not _nicks_cache or not sire_bango or not bms_bango:
        return 0.0, None

    info = _nicks_cache["lookup"].get((sire_bango, bms_bango))
    if not info:
        return 0.0, None

    wr_pctl = _percentile_rank(_nicks_cache["win_rates"], info["win_rate"])
    roi_pctl = _percentile_rank(_nicks_cache["rois"], info["roi"])

    combined = wr_pctl * 0.6 + roi_pctl * 0.4
    score = round(combined * weight, 1)

    return score, {
        "sire_name": info["sire_bamei"] or "",
        "bms_name": info["bms_bamei"] or "",
        "win_rate": round(info["win_rate"], 4),
        "roi": round(info["roi"], 4),
    }


def _calc_inbreed_score(
    sandai_ketto_list: list[dict] | None, weight: float
) -> tuple[float, float, list[dict]]:
    """
    A4: sandai_ketto（14頭）からインブリードを検出しスコアを返す。
    Wright's COI: 各共通祖先の寄与 = (1/2)^(n1+n2+1)
    n1 = 父から共通祖先までの世代数, n2 = 母から共通祖先までの世代数
    _IDX_TO_GEN は馬自身から数えた世代数なので、親からの距離は gen-1。

    返却: (スコア, coi値, インブリード情報リスト)
    """
    if not sandai_ketto_list or len(sandai_ketto_list) < 14:
        return 0.0, 0.0, []

    # 父方・母方それぞれの祖先を {hanshoku_bango: [(index, gen), ...]} に整理
    sire_side: dict[str, list[tuple[int, int]]] = {}
    for idx in _SIRE_ANCESTOR_INDICES:
        entry = sandai_ketto_list[idx]
        if not isinstance(entry, dict):
            continue
        bango = (entry.get("hanshoku_toroku_bango") or "").strip()
        if not bango:
            continue
        sire_side.setdefault(bango, []).append((idx, _IDX_TO_GEN[idx]))

    dam_side: dict[str, list[tuple[int, int]]] = {}
    for idx in _DAM_ANCESTOR_INDICES:
        entry = sandai_ketto_list[idx]
        if not isinstance(entry, dict):
            continue
        bango = (entry.get("hanshoku_toroku_bango") or "").strip()
        if not bango:
            continue
        dam_side.setdefault(bango, []).append((idx, _IDX_TO_GEN[idx]))

    # 共通祖先を検出
    common = set(sire_side.keys()) & set(dam_side.keys())

    if not common:
        return 0.0, 0.0, []

    coi = 0.0
    inbreed_info = []

    for bango in common:
        # 馬名を取得（父方の最初のエントリから）
        first_sire_idx = sire_side[bango][0][0]
        bamei = (sandai_ketto_list[first_sire_idx].get("bamei") or "").strip()

        # この祖先の個別COI寄与を合算
        # Wright's COI: (1/2)^(n1+n2+1)  n1,n2 = 親から共通祖先までの世代数
        # _IDX_TO_GEN は馬自身基準なので、親からの距離 = gen - 1
        ancestor_contribution = 0.0
        for _s_idx, s_gen in sire_side[bango]:
            for _d_idx, d_gen in dam_side[bango]:
                ancestor_contribution += 0.5 ** ((s_gen - 1) + (d_gen - 1) + 1)
        coi += ancestor_contribution

        # クロス表記を生成（例: "2×3"）
        s_gens = sorted(set(g for _, g in sire_side[bango]))
        d_gens = sorted(set(g for _, g in dam_side[bango]))
        cross = "×".join(str(g) for g in sorted(s_gens + d_gens))

        inbreed_info.append({
            "bamei": bamei,
            "hanshoku_bango": bango,
            "cross": cross,
            "coi_contribution": round(ancestor_contribution, 6),
        })

    # COI → スコア変換（線形クリップ: 0〜_COI_NORMALIZE_MAX → 0〜1）
    normalized = min(1.0, coi / _COI_NORMALIZE_MAX)
    score = round(normalized * weight, 1)

    return score, coi, inbreed_info


# ============================================================
# パース関数
# ============================================================

def parse_sandai_ketto(sandai_ketto_str: str | None) -> tuple[str | None, str | None]:
    """
    sandai_ketto文字列から父(sire)・母父(BMS)の繁殖登録番号を抽出する。
    返却: (sire_bango, bms_bango)
    """
    sire_bango = None
    bms_bango = None

    if sandai_ketto_str:
        try:
            try:
                ketto_list = json.loads(sandai_ketto_str)
            except (json.JSONDecodeError, TypeError):
                ketto_list = ast.literal_eval(sandai_ketto_str)
            if not isinstance(ketto_list, list):
                return None, None
            if len(ketto_list) > 0 and isinstance(ketto_list[0], dict):
                sire_bango = ketto_list[0].get("hanshoku_toroku_bango", "").strip() or None
            if len(ketto_list) > 4 and isinstance(ketto_list[4], dict):
                bms_bango = ketto_list[4].get("hanshoku_toroku_bango", "").strip() or None
        except (ValueError, SyntaxError, RecursionError, MemoryError, TypeError, AttributeError, KeyError):
            pass

    return sire_bango, bms_bango


def parse_sandai_ketto_full(sandai_ketto_str: str | None) -> list[dict] | None:
    """
    sandai_ketto文字列から全14頭の血統情報リストを返す。
    返却: [{hanshoku_toroku_bango: str, bamei: str}, ...] (14要素) or None
    """
    if not sandai_ketto_str:
        return None

    try:
        try:
            ketto_list = json.loads(sandai_ketto_str)
        except (json.JSONDecodeError, TypeError):
            ketto_list = ast.literal_eval(sandai_ketto_str)
        if isinstance(ketto_list, list) and len(ketto_list) >= 14:
            return ketto_list
    except (ValueError, SyntaxError, RecursionError, MemoryError, TypeError, AttributeError, KeyError):
        pass

    return None


# ============================================================
# メインスコア計算
# ============================================================

def calc_bloodline_score(
    sire_bango: str | None,
    bms_bango: str | None,
    sandai_ketto_list: list[dict] | None = None,
) -> dict:
    """
    1頭分のカテゴリAスコアを計算して返す。
    DBアクセスなし — 事前にパース済みの繁殖番号を受け取る。
    事前に ensure_percentile_cache() / ensure_nicks_cache() を呼んでおくこと。

    返却例:
    {
        "A1": 25.2, "A2": 14.5, "A3": 5.6, "A4": 4.2, "A5": 0.0,
        "total": 49.5,
        "sire_info": {...}, "bms_info": {...},
        "nicks_info": {...}, "inbreed_info": [...]
    }
    """
    if not _percentile_cache:
        logger.warning("パーセンタイルキャッシュが未初期化です。ensure_percentile_cache()を呼んでください。")

    # A1: 父成績
    a1_score, sire_info = _calc_sub_score("sire", sire_bango, DEFAULT_WEIGHTS["A1"])
    # A2: BMS成績
    a2_score, bms_info = _calc_sub_score("bms", bms_bango, DEFAULT_WEIGHTS["A2"])
    # A3: ニックス
    a3_score, nicks_info = _calc_nicks_score(sire_bango, bms_bango, DEFAULT_WEIGHTS["A3"])
    # A4: インブリード / A5: アウトブリード（排他）
    a4_score, coi, inbreed_info = _calc_inbreed_score(sandai_ketto_list, DEFAULT_WEIGHTS["A4"])
    a5_score = DEFAULT_WEIGHTS["A5"] if (sandai_ketto_list and coi == 0.0) else 0.0

    total = round(a1_score + a2_score + a3_score + a4_score + a5_score, 1)

    return {
        "A1": a1_score,
        "A2": a2_score,
        "A3": a3_score,
        "A4": a4_score,
        "A5": a5_score,
        "total": total,
        "sire_info": sire_info,
        "bms_info": bms_info,
        "nicks_info": nicks_info,
        "inbreed_info": inbreed_info,
    }

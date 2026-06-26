"""#B5 Top-1 最適化の numpy ベクトル化コア（高速スコア計算＋一致率/対数尤度）。

`run_backtest.compute_score`（1頭ずつの Python ループ）と数値的に一致するスコアを、
全頭×特徴の numpy 配列に対する行列演算で一括計算する。20,000 試行の最適化で 1 試行
あたり 15 万回の関数呼び出しを行列演算 1 発に畳み込み、所要時間を桁で短縮する。

正しさの担保: 本モジュールは compute_score の再実装のため、`test_top1_core.py` の
ゴールデンテストで pure-Python 版と全頭スコアが一致することを検証してから使う
（計測装置はバグに気づきにくい＝独立検証必須）。母集団・しきい値・ブレンド式は
run_backtest と厳密一致（唯一の差は実装方式＝ループ vs ベクトル）。

レースは (as_of_year, race_id) でソート保持するため、年範囲は連続スライスで取れる。
一致率/対数尤度は np.reduceat によるレース境界セグメント集約で完全ベクトル化する。
"""

from __future__ import annotations

import math
import sqlite3

import numpy as np

# run_backtest と同一の定数・サブ項目順（重みベクトルの順序を一致させる）。
COI_NORMALIZE_MAX = 0.15  # A4 近交係数の正規化上限
BMS_BLEND_SIRE = 0.6  # B カテゴリの sire/bms ブレンド

CACHE_TABLE = "backtest_subscore_cache"
CACHE_GENERAL = "backtest_subscore_cache_general"  # Direction A（一般戦）キャッシュ
# SQL に埋め込む table 名は固定候補のみ許可（SAST 対策・CodeRabbit PR#18 指摘）。
_ALLOWED_TABLES = {CACHE_TABLE, CACHE_GENERAL}

# 単一 wr/roi ペアのサブ項目（重みキー, カラム接頭辞）。
_SINGLE = [
    ("A1", "a1"),
    ("A2", "a2"),
    ("A3", "a3"),
    ("C1", "c1"),
    ("C2", "c2"),
    ("E1", "e1"),
    ("E2", "e2"),
]
# sire/bms ブレンドのサブ項目（B1-B4）。
_B = [("B1", "b1"), ("B2", "b2"), ("B3", "b3"), ("B4", "b4")]

_EPS = 1e-12  # 同点判定の許容誤差


def load_arrays(
    conn: sqlite3.Connection, table: str = CACHE_TABLE, year_max: int | None = None
) -> dict:
    """キャッシュ全行を (as_of_year, race_id) 昇順で読み、numpy 配列群に変換する。

    table: 読み込むキャッシュ表（既定=新馬戦の backtest_subscore_cache）。一般戦
    （Direction A）の backtest_subscore_cache_general を指定するとスピード列も読む。
    year_max: 指定時は as_of_year<=year_max の行のみ読む（OOSをSQLで封印＝金庫ルール）。

    返却 dict: 各特徴の wr/roi/mask、a4_col/a5_col（blend 非依存）、won/odds/ninki、
    レース境界（race_start: R+1, race_year: R）、race_id/umaban（ゴールデン照合用）。
    一般戦キャッシュ時は sp_*（生値）/ sp_*_pctl（as-of 百分位）/ sp_*_m（マスク）も。
    """
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"未対応のキャッシュ表: {table}")
    order = "ORDER BY as_of_year, race_id, umaban"
    if year_max is None:
        cur = conn.execute(f"SELECT * FROM {table} {order}")
    else:
        cur = conn.execute(
            f"SELECT * FROM {table} WHERE as_of_year <= ? {order}", (int(year_max),)
        )
    names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    cols = {n: [row[i] for row in rows] for i, n in enumerate(names)}

    def farr(name: str) -> np.ndarray:
        return np.array(
            [np.nan if v is None else float(v) for v in cols[name]], dtype=np.float64
        )

    def iarr(name: str) -> np.ndarray:
        return np.array(
            [0 if v is None else int(v) for v in cols[name]], dtype=np.int64
        )

    a: dict = {}
    for _, k in _SINGLE:
        a[f"{k}_wr"] = farr(f"{k}_wr")
        a[f"{k}_roi"] = farr(f"{k}_roi")
        a[f"{k}_m"] = ~np.isnan(a[f"{k}_wr"])
    for _, k in _B:
        for role in ("sire", "bms"):
            a[f"{k}_{role}_wr"] = farr(f"{k}_{role}_wr")
            a[f"{k}_{role}_roi"] = farr(f"{k}_{role}_roi")
            a[f"{k}_{role}_m"] = ~np.isnan(a[f"{k}_{role}_wr"])
    for role in ("owner", "breeder"):
        a[f"c3_{role}_wr"] = farr(f"c3_{role}_wr")
        a[f"c3_{role}_roi"] = farr(f"c3_{role}_roi")
        a[f"c3_{role}_m"] = ~np.isnan(a[f"c3_{role}_wr"])

    coi = farr("a4_coi")
    a["a4_col"] = np.where(
        ~np.isnan(coi), np.minimum(1.0, coi / COI_NORMALIZE_MAX), 0.0
    )
    out = farr("a5_outbreed")
    a["a5_col"] = np.where(out == 1.0, 1.0, 0.0)
    # 信号診断（analyze_subscore_signal）用に raw（nan=データ無し）も公開する。
    # a4_col/a5_col は「データ無し」と「coi=0/非アウト」を共に 0 に潰すため、
    # 「データのある馬だけでAUCを測る」マスク作りには raw が要る。
    a["a4_coi"] = coi
    a["a5_outbreed"] = out

    # 着順（chakujun は TEXT "01".."NN"。"00"/空/異常は 0=無効着）。TOP-N 入賞ラベル用。
    def chk(v) -> int:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return 0
        return iv if iv >= 1 else 0

    a["chaku"] = np.array([chk(v) for v in cols["chakujun"]], dtype=np.int64)

    a["won"] = iarr("won")
    a["ninki"] = iarr("ninki")
    a["odds"] = farr("tansho_odds")
    a["race_id"] = np.array(cols["race_id"], dtype=object)
    a["umaban"] = np.array(cols["umaban"], dtype=object)

    # スピード列（一般戦キャッシュのみ存在）。生値=AUC順序用、pctl=束ねスコア用。
    for sp in ("sp_soha_best", "sp_soha_recent", "sp_soha_avg", "sp_ato3f_best"):
        if sp in cols:
            a[sp] = farr(sp)
            a[f"{sp}_pctl"] = farr(f"{sp}_pctl")
            a[f"{sp}_m"] = ~np.isnan(a[sp])

    year = iarr("as_of_year")
    rid = a["race_id"]
    n = len(rid)
    if n == 0:
        a["race_start"] = np.array([0], dtype=np.int64)
        a["race_year"] = np.array([], dtype=np.int64)
        a["N"], a["R"] = 0, 0
        return a
    # ソートキー (as_of_year, race_id) と境界キーを一致させる。race_id は16桁で年を含み
    # 一意のため現状 race_id だけでも割れないが、両キーで判定し将来の取り違えを防ぐ。
    change = (year[1:] != year[:-1]) | (rid[1:] != rid[:-1])
    starts = np.concatenate(([0], np.nonzero(change)[0] + 1, [n])).astype(np.int64)
    a["race_start"] = starts
    a["race_year"] = year[starts[:-1]]
    a["N"], a["R"] = n, len(starts) - 1
    return a


def _pair(
    wr: np.ndarray, roi: np.ndarray, mask: np.ndarray, blend: float
) -> np.ndarray:
    """wr/roi パーセンタイルを blend 合成。データ無し(mask=False)は 0。"""
    return np.where(mask, wr * blend + roi * (1.0 - blend), 0.0)


def score_all(a: dict, weights: dict, blend: float) -> np.ndarray:
    """全頭の総合スコア（compute_score のベクトル化・厳密一致）。"""
    s = np.zeros(a["N"], dtype=np.float64)
    for sub, k in _SINGLE:
        s += _pair(a[f"{k}_wr"], a[f"{k}_roi"], a[f"{k}_m"], blend) * weights[sub]
    s += a["a4_col"] * weights["A4"]
    s += a["a5_col"] * weights["A5"]
    for sub, k in _B:
        sp = _pair(a[f"{k}_sire_wr"], a[f"{k}_sire_roi"], a[f"{k}_sire_m"], blend)
        bp = _pair(a[f"{k}_bms_wr"], a[f"{k}_bms_roi"], a[f"{k}_bms_m"], blend)
        sm, bm = a[f"{k}_sire_m"], a[f"{k}_bms_m"]
        both = sp * BMS_BLEND_SIRE + bp * (1.0 - BMS_BLEND_SIRE)
        combined = np.where(sm & bm, both, np.where(sm, sp, np.where(bm, bp, 0.0)))
        s += combined * weights[sub]
    op = _pair(a["c3_owner_wr"], a["c3_owner_roi"], a["c3_owner_m"], blend)
    brp = _pair(a["c3_breeder_wr"], a["c3_breeder_roi"], a["c3_breeder_m"], blend)
    s += (op + brp) * 0.5 * weights["C3"]  # owner/breeder に C3/2 ずつ
    # スピード（一般戦のみ。weights["SP"] があれば走破直近 as-of pctl を 0-1 特徴として束ねる）。
    # M1 診断で走破直近が最強だったため代表1次元として採用。データ無し(過去走ゼロ)は 0。
    sp_col = a.get("sp_soha_recent_pctl")
    if sp_col is not None and "SP" in weights:
        s += np.where(np.isnan(sp_col), 0.0, sp_col) * weights["SP"]
    return s


def _range_slice(a: dict, y_start: int, y_end: int):
    """年範囲 [y_start, y_end] のレース・頭スライスとローカル境界を返す。

    レースは as_of_year 昇順なので年範囲は連続レース [r0:r1) ＝連続頭 [h0:h1)。
    返却: (h0, h1, seg, counts, n_races)。seg=ローカル境界の開始 index 列（reduceat 用）。
    """
    ry = a["race_year"]
    r0 = int(np.searchsorted(ry, y_start, "left"))
    r1 = int(np.searchsorted(ry, y_end, "right"))
    if r1 <= r0:
        return None
    starts = a["race_start"][r0 : r1 + 1]
    h0, h1 = int(starts[0]), int(starts[-1])
    local = starts - h0
    return h0, h1, local[:-1], np.diff(local), (r1 - r0)


def top1_match_rate(a: dict, scores: np.ndarray, y_start: int, y_end: int):
    """道A: Top-1 一致率と分母レース数。最高スコアが1頭に定まりそれが1着なら一致。

    同点1位が2頭以上のレースは不一致(0)（分母には残す）＝全馬同点の水増しは一致率0に潰れる。
    """
    sl = _range_slice(a, y_start, y_end)
    if sl is None:
        return 0.0, 0
    h0, h1, seg, counts, n = sl
    sc = scores[h0:h1]
    won = a["won"][h0:h1]
    race_max = np.maximum.reduceat(sc, seg)
    max_per_head = np.repeat(race_max, counts)
    is_top = sc >= max_per_head - _EPS
    top_count = np.add.reduceat(is_top.astype(np.int64), seg)
    top_won = np.add.reduceat((is_top & (won == 1)).astype(np.int64), seg)
    match = int(np.sum((top_count == 1) & (top_won >= 1)))
    return match / n, n


def top1_loglik(
    a: dict, scores: np.ndarray, y_start: int, y_end: int, beta: float
) -> float:
    """道B: 各レースで1着馬に softmax(β*score) が与える確率の平均対数尤度。

    1着が複数（同着）のレースは勝者の対数確率を平均。1着不在レースは分母から除外。
    """
    sl = _range_slice(a, y_start, y_end)
    if sl is None:
        return -math.inf
    h0, h1, seg, counts, _ = sl
    sc = scores[h0:h1]
    won = a["won"][h0:h1]
    race_max = np.maximum.reduceat(sc, seg)
    max_per_head = np.repeat(race_max, counts)
    z = beta * (sc - max_per_head)  # max 減算で exp の overflow を回避
    denom_per_head = np.repeat(np.add.reduceat(np.exp(z), seg), counts)
    # log-domain で計算（ex/denom だと勝馬 exp が underflow し有限な対数尤度が -inf に潰れる）。
    logp = np.where(won == 1, z - np.log(denom_per_head), 0.0)
    sum_logp = np.add.reduceat(logp, seg)
    nwin = np.add.reduceat((won == 1).astype(np.int64), seg)
    valid = nwin > 0
    n = int(np.sum(valid))
    if n == 0:
        return -math.inf
    return float(np.sum(sum_logp[valid] / nwin[valid]) / n)


def favorite_match_rate(a: dict, y_start: int, y_end: int):
    """参照ベースライン: 1番人気の的中率（市場の Top-1）と分母レース数。"""
    sl = _range_slice(a, y_start, y_end)
    if sl is None:
        return 0.0, 0
    h0, h1, seg, _, n = sl
    ninki = a["ninki"][h0:h1]
    won = a["won"][h0:h1]
    is_fav = ninki == 1
    fav_won = np.add.reduceat((is_fav & (won == 1)).astype(np.int64), seg)
    fav_any = np.add.reduceat(is_fav.astype(np.int64), seg)
    valid = fav_any >= 1
    nv = int(np.sum(valid))
    hit = int(np.sum((fav_won >= 1) & valid))
    return (hit / nv if nv else 0.0), nv

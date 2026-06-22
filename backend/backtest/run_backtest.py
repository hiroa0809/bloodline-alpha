"""ウォークフォワード・バックテスト実行（Phase 1-B2 / #B2）。

前計算済みの `backtest_subscore_cache`（#B1）を読み、重みを掛けて総合スコアを
再現 → 各レースで「スコア1位の馬に単勝」する戦略の ROI / 的中率を、アンカー型
ウォークフォワードの各フォールド（IS / OOS-1〜3）で評価する。

なぜキャッシュを使うか（CLAUDE.md「バックテスト方法論」/「最適化アルゴリズム」）:
  as-of 集計（重み非依存の重い処理）は #B1 で一度だけ前計算済み。本スクリプトは
  『重み × サブスコア』のベクトル演算だけで総合スコアを出すため再集計不要。これにより
  #B3 の重み最適化は本ファイルの evaluate() を目的関数として高速に回せる。

総合スコアの合成はライブ（backend/app/services/*_score.py）と厳密一致:
  - 各サブスコア: combined = 勝率pctl×wr_blend + ROIpctl×(1-wr_blend)、点数 = combined×重み
  - B1-B4: sire/bms を 0.6/0.4 ブレンド（片方欠落時はある方のみ）
  - C3: owner/breeder に C3/2 ずつ配分
  - A4: min(1, coi/0.15)×重み、A5: 近交係数0なら満点
  欠落サブスコア（データ無し）は 0 点（ライブと同一）。

評価戦略（#B2 でユーザー確定 2026-06-22）: 各レースでスコア最高の1頭に単勝1単位。
  ROI = Σ(勝った馬の単勝オッズ) / 賭けレース数、的中率 = 勝ち数 / 賭けレース数。
  参考として「1番人気に単勝」のベースラインも併記する。

使い方:
    python backend/backtest/run_backtest.py
    python backend/backtest/run_backtest.py --wr-blend 0.5   # 勝率/ROI ブレンド比を変更
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = _BACKEND_DIR / "bloodline.db"
CACHE_TABLE = "backtest_subscore_cache"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# アンカー型ウォークフォワード分割（CLAUDE.md「バックテスト方法論」）。
# (ラベル, 開始年, 終了年, 評価対象か)。ウォームアップは統計蓄積用で評価しない。
FOLDS = [
    ("ウォームアップ", 1986, 1992, False),
    ("IS", 1993, 2013, True),
    ("OOS-1", 2014, 2017, True),
    ("OOS-2", 2018, 2021, True),
    ("OOS-3", 2022, 2025, True),
]

# ベースライン重み（ライブ各 *_score.py の DEFAULT_WEIGHTS と一致。#B3 で最適化）
DEFAULT_WEIGHTS = {
    "A1": 28,
    "A2": 16,
    "A3": 8,
    "A4": 7,
    "A5": 6,
    "B1": 5,
    "B2": 5,
    "B3": 4,
    "B4": 3,
    "C1": 4,
    "C2": 4,
    "C3": 2,
    "E1": 3,
    "E2": 2,
}

# 勝率/ROI ブレンド比（ライブと一致: 勝率0.6 / ROI0.4）。#B3 で最適化可。
DEFAULT_WR_BLEND = 0.6
# B カテゴリの sire/bms ブレンド（ライブ race_condition_score と一致）。
BMS_BLEND_SIRE = 0.6
# A4 近交係数の正規化上限（bloodline_score._COI_NORMALIZE_MAX と一致）。
COI_NORMALIZE_MAX = 0.15


def _pair(wr: float | None, roi: float | None, wr_blend: float) -> float | None:
    """勝率pctl / ROIpctl のペアを 1 つの combined 値へ。データ無しは None。

    キャッシュでは wr/roi は同時に埋まる/欠ける（score_runner と同仕様）ため wr で判定。
    """
    if wr is None:
        return None
    return wr * wr_blend + roi * (1.0 - wr_blend)


def compute_score(row: sqlite3.Row, weights: dict, wr_blend: float) -> float:
    """1頭のキャッシュ行 → 総合スコア。ライブのサブスコア合成を再現する。"""
    s = 0.0
    # A1/A2/A3・C1/C2・E1/E2: 単一の wr/roi ペア
    for sub, key in (
        ("A1", "a1"),
        ("A2", "a2"),
        ("A3", "a3"),
        ("C1", "c1"),
        ("C2", "c2"),
        ("E1", "e1"),
        ("E2", "e2"),
    ):
        p = _pair(row[f"{key}_wr"], row[f"{key}_roi"], wr_blend)
        if p is not None:
            s += p * weights[sub]

    # A4 近交（線形クリップ）/ A5 アウトブリード（近交0で満点）
    coi = row["a4_coi"]
    if coi is not None:
        s += min(1.0, coi / COI_NORMALIZE_MAX) * weights["A4"]
    if row["a5_outbreed"] == 1:
        s += weights["A5"]

    # B1-B4: sire/bms をブレンド（片方欠落時はある方のみ。ライブと一致）
    for sub, key in (("B1", "b1"), ("B2", "b2"), ("B3", "b3"), ("B4", "b4")):
        sp = _pair(row[f"{key}_sire_wr"], row[f"{key}_sire_roi"], wr_blend)
        bp = _pair(row[f"{key}_bms_wr"], row[f"{key}_bms_roi"], wr_blend)
        if sp is not None and bp is not None:
            combined = sp * BMS_BLEND_SIRE + bp * (1.0 - BMS_BLEND_SIRE)
        elif sp is not None:
            combined = sp
        elif bp is not None:
            combined = bp
        else:
            combined = None
        if combined is not None:
            s += combined * weights[sub]

    # C3: owner/breeder に C3/2 ずつ
    half = weights["C3"] / 2.0
    for key in ("c3_owner", "c3_breeder"):
        p = _pair(row[f"{key}_wr"], row[f"{key}_roi"], wr_blend)
        if p is not None:
            s += p * half

    return s


def load_races(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """クリーン期間の全キャッシュ行を race_id 単位でグループ化して返す。"""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {CACHE_TABLE}").fetchall()
    races: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        races.setdefault(r["race_id"], []).append(r)
    logger.info(f"対象: {len(rows):,} 頭 / {len(races):,} レース")
    return races


def count_bettable_races(
    races: dict[str, list[sqlite3.Row]], year_start: int, year_end: int
) -> int:
    """指定年範囲で単勝オッズを持つ馬が1頭以上いるレース数（重み非依存のサンプルサイズ）。

    evaluate の n は argmax で選ばれた馬のオッズ有無に依存し重み依存になるため、
    最適化対象の有無判定や fold の加重平均では本関数の重み非依存カウントを使う。
    """
    return sum(
        1
        for runners in races.values()
        if year_start <= runners[0]["as_of_year"] <= year_end
        and any(r["tansho_odds"] is not None for r in runners)
    )


def evaluate(
    races: dict[str, list[sqlite3.Row]],
    year_start: int,
    year_end: int,
    weights: dict,
    wr_blend: float,
) -> dict:
    """指定年範囲のレースで戦略（スコア1位単勝）と1番人気ベースラインを評価する。"""
    n = wins = 0
    ret = 0.0
    fav_n = fav_wins = 0
    fav_ret = 0.0

    for runners in races.values():
        y = runners[0]["as_of_year"]
        if not (year_start <= y <= year_end):
            continue

        # スコア1位（同点は馬番昇順で安定化: -umaban が大きい=小さい馬番が優先）
        pick = max(
            runners,
            key=lambda r: (
                compute_score(r, weights, wr_blend),
                -_safe_int(r["umaban"]),
            ),
        )
        if pick["tansho_odds"] is not None:
            n += 1
            if pick["won"] == 1:
                wins += 1
                ret += pick["tansho_odds"]

        # ベースライン: 1番人気
        fav = next((r for r in runners if r["ninki"] == 1), None)
        if fav and fav["tansho_odds"] is not None:
            fav_n += 1
            if fav["won"] == 1:
                fav_wins += 1
                fav_ret += fav["tansho_odds"]

    return {
        "n": n,
        "roi": ret / n if n else 0.0,
        "hit": wins / n if n else 0.0,
        "fav_n": fav_n,
        "fav_roi": fav_ret / fav_n if fav_n else 0.0,
        "fav_hit": fav_wins / fav_n if fav_n else 0.0,
    }


def _safe_int(v: str | None) -> int:
    """馬番文字列を int に。失敗時は 0（同点タイブレーク用）。"""
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def main() -> None:
    """キャッシュを読み、各フォールドの ROI/的中率を表形式で出力する。"""
    ap = argparse.ArgumentParser(description="ウォークフォワード・バックテスト（#B2）")
    ap.add_argument(
        "--wr-blend",
        type=float,
        default=DEFAULT_WR_BLEND,
        help="勝率/ROI ブレンド比の勝率側（既定 0.6）",
    )
    args = ap.parse_args()

    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    try:
        races = load_races(conn)
    finally:
        conn.close()

    logger.info(
        f"=== ウォークフォワード・バックテスト（スコア1位 単勝） / "
        f"勝率ブレンド {args.wr_blend} ==="
    )
    header = (
        f"{'fold':<14}{'期間':<12}{'N(レース)':>10}"
        f"{'スコア1位ROI':>14}{'的中率':>9}  | "
        f"{'1番人気ROI':>12}{'的中率':>9}"
    )
    logger.info(header)
    for label, ys, ye, evaluated in FOLDS:
        m = evaluate(races, ys, ye, DEFAULT_WEIGHTS, args.wr_blend)
        tag = label if evaluated else f"{label}(参考)"
        logger.info(
            f"{tag:<14}{f'{ys}-{ye}':<12}{m['n']:>10,}"
            f"{m['roi'] * 100:>13.1f}%{m['hit'] * 100:>8.1f}%  | "
            f"{m['fav_roi'] * 100:>11.1f}%{m['fav_hit'] * 100:>8.1f}%"
        )


if __name__ == "__main__":
    main()

"""#B5 Stage A: オッズ帯による選択的ベット診断 + IS内ウォークフォワードCV検証。

#B2 は「各レースでスコア1位の馬に“必ず”単勝」を打ち、IS の的中率は約16%・ROIは
概ね赤字だった。#B3 で重み最適化は過学習と判明（重みは利くレバーでない）。そこで
ユーザー提案の方向＝全レース強制ベットをやめ「勝てる条件のレースだけ賭ける」選択的
ベットを検証する。本スクリプトは「スコア1位の馬の単勝オッズがどの帯にあるか」で
的中率/ROIを細分し、+EV になるオッズ帯（連続窓を含む）を探す（#B5 の柱 C の第一歩）。

金庫ルール（CLAUDE.md「バックテスト方法論」）を厳守:
  - 探索は IS（1993-2013）限定。OOS-1〜3 は本スクリプトで一切評価しない（封印）。
  - オッズ帯境界は事前登録の固定値（ODDS_BANDS）。OOS結果を見て後から動かさない。
  - スコア重みは固定（DEFAULT_WEIGHTS / wr_blend=0.6）。再最適化しない＝#B3の過学習を
    上塗りしない。#B5 はスコアの上に乗る「ベットフィルタ層」だけを検証する。

なぜ速いか: 重い as-of 集計は #B1 で前計算済み。本スクリプトは run_backtest と同様に
「重み×サブスコア」のベクトル演算でスコア1位を選ぶだけ＝Optuna不要・数秒で完了。

2部構成:
  Part 1 診断 — IS全体でスコア1位 pick をオッズ帯別に集計（N/的中率/ROI）。全帯合計が
    #B2 の IS 的中率（約16%）を再現する（再利用ロジックの正しさの担保）。
  Part 2 IS内CV — 学習区間で +EV 帯（ROI≥min-roi かつ N≥min-n）を選び、未見の検証区間に
    適用して検証ROI/的中/被覆率を測る。学習ROIとの差＝過学習ギャップ。検証ROIを被覆
    レース数で加重平均したものが「過学習補正後の実力見積もり」。

使い方:
    python backend/backtest/analyze_odds_bands.py
    python backend/backtest/analyze_odds_bands.py --wr-blend 0.5 --min-roi 1.05 --min-n 50
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from backtest.run_backtest import (  # noqa: E402
    DEFAULT_WEIGHTS,
    DEFAULT_WR_BLEND,
    _safe_int,
    compute_score,
    load_races,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "odds_band_report.json"

# IS（学習・重み最適化区間。CLAUDE.md「バックテスト方法論」）。OOS は封印。
IS_START, IS_END = 1993, 2013

# IS内ウォークフォワードCV分割（学習開始, 学習終了, 検証開始, 検証終了）。
# robustness.py の CV_FOLDS と同一値（optuna 依存を避けるため import せずローカル定義）。
CV_FOLDS = [
    (1993, 2005, 2006, 2008),
    (1993, 2008, 2009, 2011),
    (1993, 2011, 2012, 2013),
]

# 事前登録オッズ帯（OOSを見る前に固定）。(下限含む, 上限含まない, ラベル)。
ODDS_BANDS = [
    (1.0, 2.0, "1.0-2.0"),
    (2.0, 3.0, "2.0-3.0"),
    (3.0, 5.0, "3.0-5.0"),
    (5.0, 10.0, "5.0-10.0"),
    (10.0, 20.0, "10.0-20.0"),
    (20.0, float("inf"), "20.0+"),
]
BAND_LABELS = [label for _, _, label in ODDS_BANDS]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def band_of(odds: float) -> str | None:
    """単勝オッズ → 事前登録帯ラベル。範囲外（<1.0）は None。"""
    for lo, hi, label in ODDS_BANDS:
        if lo <= odds < hi:
            return label
    return None


def collect_picks(
    races: dict[str, list[sqlite3.Row]],
    year_start: int,
    year_end: int,
    weights: dict,
    wr_blend: float,
) -> list[tuple[float, int]]:
    """指定年範囲の各レースでスコア1位 pick を選び (オッズ, 勝ち0/1) を集める。

    pick選定は run_backtest.evaluate と同一（同点は馬番昇順で安定化）。オッズ欠落 pick は
    除外（賭けられないため）＝ run_backtest と同じ扱い。
    """
    picks: list[tuple[float, int]] = []
    for runners in races.values():
        y = runners[0]["as_of_year"]
        if not (year_start <= y <= year_end):
            continue
        pick = max(
            runners,
            key=lambda r: (
                compute_score(r, weights, wr_blend),
                -_safe_int(r["umaban"]),
            ),
        )
        odds = pick["tansho_odds"]
        if odds is not None:
            picks.append((float(odds), 1 if pick["won"] == 1 else 0))
    return picks


def collect_favorites(
    races: dict[str, list[sqlite3.Row]], year_start: int, year_end: int
) -> list[tuple[float, int]]:
    """参考ベースライン: 1番人気の (オッズ, 勝ち0/1) を集める（重み非依存）。"""
    picks: list[tuple[float, int]] = []
    for runners in races.values():
        y = runners[0]["as_of_year"]
        if not (year_start <= y <= year_end):
            continue
        fav = next((r for r in runners if r["ninki"] == 1), None)
        if fav and fav["tansho_odds"] is not None:
            picks.append((float(fav["tansho_odds"]), 1 if fav["won"] == 1 else 0))
    return picks


def _metrics(n: int, wins: int, ret: float) -> dict:
    """N/勝ち数/オッズ累積 → N・的中率・ROI（ROIは勝ち馬オッズ累積/N、100%=損益分岐）。"""
    return {
        "n": n,
        "hit": wins / n if n else 0.0,
        "roi": ret / n if n else 0.0,
    }


def aggregate_by_band(picks: list[tuple[float, int]]) -> dict[str, dict]:
    """pick 群を帯別に集計。各帯 {n, hit, roi}。"""
    raw = {label: [0, 0, 0.0] for label in BAND_LABELS}  # [n, wins, ret]
    for odds, won in picks:
        label = band_of(odds)
        if label is None:
            continue
        raw[label][0] += 1
        if won:
            raw[label][1] += 1
            raw[label][2] += odds
    return {label: _metrics(*raw[label]) for label in BAND_LABELS}


def eval_all(picks: list[tuple[float, int]]) -> dict:
    """全 pick を強制ベット（フィルタ無し）した時の N・的中率・ROI。"""
    n = len(picks)
    wins = sum(w for _, w in picks)
    ret = sum(o for o, w in picks if w)
    return _metrics(n, wins, ret)


def eval_with_bands(picks: list[tuple[float, int]], bands: set[str]) -> dict:
    """選択帯に入る pick だけ賭けた時の N(被覆数)・的中率・ROI。"""
    n = wins = 0
    ret = 0.0
    for odds, won in picks:
        if band_of(odds) in bands:
            n += 1
            if won:
                wins += 1
                ret += odds
    return _metrics(n, wins, ret)


def select_plus_ev_bands(
    train_picks: list[tuple[float, int]], min_roi: float, min_n: int
) -> list[str]:
    """学習区間で ROI≥min_roi かつ N≥min_n の帯を「賭ける帯集合」として選ぶ（ルール学習）。"""
    agg = aggregate_by_band(train_picks)
    return [
        label
        for label in BAND_LABELS
        if agg[label]["n"] >= min_n and agg[label]["roi"] >= min_roi
    ]


def _log_band_table(title: str, agg: dict[str, dict], total: dict) -> None:
    """帯別テーブルをログ出力。帯外（オッズ0=異常値）は別行で明示し合計と整合させる。"""
    logger.info(title)
    logger.info(f"  {'帯':<12}{'N':>8}{'的中率':>9}{'ROI':>10}")
    band_n = 0
    for label in BAND_LABELS:
        m = agg[label]
        band_n += m["n"]
        logger.info(
            f"  {label:<12}{m['n']:>8,}{m['hit'] * 100:>8.1f}%{m['roi'] * 100:>9.1f}%"
        )
    # オッズ0（出走取消等の異常値）は帯に入らないが run_backtest 同様 N に算入されるため、
    # 合計＝帯別合計＋帯外 となるよう帯外行を明示する。
    out_n = total["n"] - band_n
    if out_n:
        logger.info(f"  {'対象外(odds0)':<12}{out_n:>8,}{'—':>9}{'—':>10}")
    logger.info(
        f"  {'合計':<12}{total['n']:>8,}{total['hit'] * 100:>8.1f}%"
        f"{total['roi'] * 100:>9.1f}%"
    )


def run_diagnostic(races: dict, weights: dict, wr_blend: float) -> dict:
    """Part 1: IS全体でスコア1位 pick と1番人気をオッズ帯別に集計する。"""
    logger.info(f"=== Part 1 診断: オッズ帯別（IS {IS_START}-{IS_END}） ===")
    score_picks = collect_picks(races, IS_START, IS_END, weights, wr_blend)
    fav_picks = collect_favorites(races, IS_START, IS_END)

    score_agg = aggregate_by_band(score_picks)
    score_total = eval_all(score_picks)
    fav_agg = aggregate_by_band(fav_picks)
    fav_total = eval_all(fav_picks)

    _log_band_table("スコア1位 単勝:", score_agg, score_total)
    _log_band_table("（参考）1番人気 単勝:", fav_agg, fav_total)

    # 帯外（オッズ0）件数。合計＝帯別合計＋帯外 を JSON でも追跡可能にする。
    score_excluded = score_total["n"] - sum(score_agg[b]["n"] for b in BAND_LABELS)
    fav_excluded = fav_total["n"] - sum(fav_agg[b]["n"] for b in BAND_LABELS)
    return {
        "score_top1": {
            "by_band": score_agg,
            "total": score_total,
            "excluded_zero_odds": score_excluded,
        },
        "favorite": {
            "by_band": fav_agg,
            "total": fav_total,
            "excluded_zero_odds": fav_excluded,
        },
    }


def run_cv(
    races: dict, weights: dict, wr_blend: float, min_roi: float, min_n: int
) -> dict:
    """Part 2: IS内ウォークフォワードCV。学習で +EV 帯を選び未見の検証区間で採点する。"""
    logger.info(
        f"=== Part 2 IS内CV: 学習で +EV帯選択(ROI≥{min_roi * 100:.0f}% & N≥{min_n}) "
        f"→ 検証で採点（{len(CV_FOLDS)}フォールド） ==="
    )
    folds = []
    for tr_s, tr_e, va_s, va_e in CV_FOLDS:
        train_picks = collect_picks(races, tr_s, tr_e, weights, wr_blend)
        valid_picks = collect_picks(races, va_s, va_e, weights, wr_blend)
        bands = select_plus_ev_bands(train_picks, min_roi, min_n)
        bands_set = set(bands)

        train_sel = eval_with_bands(train_picks, bands_set)
        valid_sel = eval_with_bands(valid_picks, bands_set)
        valid_all = eval_all(valid_picks)
        coverage = valid_sel["n"] / valid_all["n"] if valid_all["n"] else 0.0
        gap = train_sel["roi"] - valid_sel["roi"]

        folds.append(
            {
                "train": [tr_s, tr_e],
                "valid": [va_s, va_e],
                "selected_bands": bands,
                "train_sel": train_sel,
                "valid_sel": valid_sel,
                "valid_all": valid_all,
                "coverage": coverage,
                "gap": gap,
            }
        )
        logger.info(
            f"  学習{tr_s}-{tr_e}→検証{va_s}-{va_e}: 選択帯={bands or '(なし)'}"
        )
        logger.info(
            f"    学習(選択)ROI {train_sel['roi'] * 100:.1f}% / "
            f"検証(選択)ROI {valid_sel['roi'] * 100:.1f}% "
            f"(過学習ギャップ {gap * 100:+.1f}pt) / "
            f"被覆 {valid_sel['n']}/{valid_all['n']} ({coverage * 100:.0f}%) / "
            f"検証(全帯)ROI {valid_all['roi'] * 100:.1f}%"
        )

    # 検証(選択)ROIを被覆レース数で加重平均＝過学習補正後の実力見積もり。
    # 被覆が0のfoldは賭けが無く実力に寄与しないため自然に除外される。
    total_cov_n = sum(f["valid_sel"]["n"] for f in folds)
    total_all_n = sum(f["valid_all"]["n"] for f in folds)
    valid_sel_overall = (
        sum(f["valid_sel"]["roi"] * f["valid_sel"]["n"] for f in folds) / total_cov_n
        if total_cov_n
        else 0.0
    )
    valid_all_overall = (
        sum(f["valid_all"]["roi"] * f["valid_all"]["n"] for f in folds) / total_all_n
        if total_all_n
        else 0.0
    )
    coverage_overall = total_cov_n / total_all_n if total_all_n else 0.0
    logger.info(
        f"  → 検証(選択)ROI加重平均（実力見積もり）: {valid_sel_overall * 100:.1f}% "
        f"/ 検証(全帯)ROI: {valid_all_overall * 100:.1f}% / "
        f"被覆率: {coverage_overall * 100:.0f}%"
    )
    return {
        "folds": folds,
        "valid_sel_roi_overall": valid_sel_overall,
        "valid_all_roi_overall": valid_all_overall,
        "coverage_overall": coverage_overall,
    }


def main() -> None:
    """診断とIS内CVを実行し、レポートを保存する（OOS封印）。"""
    ap = argparse.ArgumentParser(
        description="#B5 オッズ帯による選択的ベット診断＋IS内CV検証"
    )
    ap.add_argument(
        "--wr-blend",
        type=float,
        default=DEFAULT_WR_BLEND,
        help="勝率/ROIブレンド比（既定0.6）",
    )
    ap.add_argument(
        "--min-roi",
        type=float,
        default=1.0,
        help="+EV帯と見なすROI下限（既定1.0=損益分岐）",
    )
    ap.add_argument(
        "--min-n",
        type=int,
        default=30,
        help="+EV帯と見なす学習区間ベット数の下限（既定30）",
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

    diagnostic = run_diagnostic(races, DEFAULT_WEIGHTS, args.wr_blend)
    cv = run_cv(races, DEFAULT_WEIGHTS, args.wr_blend, args.min_roi, args.min_n)

    out = {
        "is_period": [IS_START, IS_END],
        "wr_blend": args.wr_blend,
        "min_roi": args.min_roi,
        "min_n": args.min_n,
        "odds_bands": BAND_LABELS,
        "diagnostic": diagnostic,
        "cross_validation": cv,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

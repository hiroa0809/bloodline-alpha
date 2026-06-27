"""Direction A M3c: 一般戦スピード図表でエッジベット運用＝黒字化の直接テスト。

M1〜M3b は全て「紙の上の目利き（AUC）」診断だった。出尽くした結論＝スピード系で一貫して残る
エッジは M2 走破(soha_recent ③0.524)＋末脚(ato3f_best ③0.526) の薄い③≈0.52-0.53 のみ。
M3c は Direction A 本来の目的＝初めて実際に賭けて「控除(~20%)を越えて黒字化(選択ROI≥100%)
するか」を直接測る。手法は既存の stage_c_edge_bet（新馬戦で実装済み・確率校正β→edge=p×オッズ−1
→しきい値τ→IS内CV）を一般戦×スピード図表に再適用するだけ（スコアは固定・再最適化しない）。

設計の鍵＝図表なし馬（過去走なし＝被覆~76%）の扱い:
  - scores: 図表なし馬は「そのレースの有効図表の最小値」で補完（速さ未証明を最下位相当に。
    softmax の確率質量を保ちつつ図表馬の p を不当に膨らませない）。
  - odds: 図表あり馬は実オッズ／図表なし馬は None（stage_c の collect_edge_bets が odds=None を
    賭け対象外にスキップ＝信号のない馬は賭けない、を既存機構そのままで実現）。

正直な見通し: ③0.52-0.53 は極薄。新馬戦 Stage C（ファンダ）は大穴バイアスで不発だった。スピード
でも控除20%を越えない可能性が高いが、これが"お金になるか"を初めて直接測る決定的テスト。

金庫: 非新馬平地・IS1993-2013限定・OOS封印（pipeline ana_end=IS_END・SQLでも封印）。β・τ・診断は
すべて IS 内。事前登録: 賭ける図表（soha_recent/ato3f_best）・補完規約（レース最小値）・図表なし馬は
賭けない・TAU_GRID/CV_FOLDS/EDGE_BUCKETS（stage_c から継承）。スコア重みは固定。既存装置は不変。

使い方:
    python backend/backtest/analyze_speed_edge_general.py
    python backend/backtest/analyze_speed_edge_general.py --smoke   # 小窓・実行可否のみ
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from backtest.speed_m2_core import (  # noqa: E402
    MIN_DAY_RUNS,
    assign_figures,
    assign_horse_features,
    build_arrays,
    load_race_info,
    load_runs,
)
from backtest.stage_c_edge_bet import (  # noqa: E402
    IS_END,
    IS_START,
    Race,
    run_cv,
    run_diagnostic,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "speed_edge_general_report.json"

SCAN_START = 1986
SMOKE_SCAN_START, SMOKE_ANA_END = 2005, 2010

# 賭ける図表＝M2でゲート通過した2つ（事前登録）。
FIGURES = [
    ("soha_recent", "走破直近(補正)・③0.524"),
    ("ato3f_best", "上り3Fbest(補正)・③0.526"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def build_speed_races(a: dict, figure_key: str) -> list[Race]:
    """スピード pipeline の `a` から、図表 figure_key をスコアにした Race リストを作る。

    図表なし馬は scores をレース有効最小値で補完し odds=None（賭け対象外）にする。
    """
    vals = a[figure_key]
    fmask = a[f"{figure_key}_m"]
    odds = a["odds"]
    chaku = a["chaku"]
    starts = a["race_start"]
    years = a["race_year"]
    out: list[Race] = []
    for i in range(len(starts) - 1):
        h0, h1 = int(starts[i]), int(starts[i + 1])
        v = vals[h0:h1]
        fm = fmask[h0:h1]
        o = odds[h0:h1]
        c = chaku[h0:h1]
        valid = fm & ~np.isnan(v)
        race_min = float(v[valid].min()) if bool(valid.any()) else 0.0
        scores: list[float] = []
        race_odds: list[float | None] = []
        won: list[int] = []
        for j in range(h1 - h0):
            has_fig = bool(valid[j])
            scores.append(float(v[j]) if has_fig else race_min)
            oj = o[j]
            bettable = has_fig and not np.isnan(oj) and oj > 0
            race_odds.append(float(oj) if bettable else None)
            won.append(1 if int(c[j]) == 1 else 0)
        win_idx = [k for k, w in enumerate(won) if w == 1]
        winner = win_idx[0] if len(win_idx) == 1 else None
        out.append(Race(int(years[i]), scores, race_odds, won, winner))
    return out


def flat_top1(races: list[Race], ys: int, ye: int) -> dict:
    """サニティ: 各レースで図表最高（=スコア最大の賭け可能馬）に単勝したROI/的中/被覆。"""
    n = wins = 0
    ret = 0.0
    for r in races:
        if not (ys <= r.year <= ye):
            continue
        best_i, best_s = None, -float("inf")
        for k, o in enumerate(r.odds):
            if o is None:
                continue
            if r.scores[k] > best_s:
                best_i, best_s = k, r.scores[k]
        if best_i is None:
            continue
        n += 1
        if r.won[best_i] == 1:
            wins += 1
            ret += r.odds[best_i]
    return {"n": n, "hit": wins / n if n else 0.0, "roi": ret / n if n else 0.0}


def _coverage(races: list[Race], ys: int, ye: int) -> dict:
    """賭け可能（図表あり）馬数・全馬数・賭け可能レース数。"""
    bettable = total = bettable_races = 0
    for r in races:
        if not (ys <= r.year <= ye):
            continue
        nb = sum(1 for o in r.odds if o is not None)
        bettable += nb
        total += len(r.odds)
        if nb > 0:
            bettable_races += 1
    return {
        "bettable_horses": bettable,
        "total_horses": total,
        "bettable_races": bettable_races,
        "coverage": bettable / total if total else 0.0,
    }


def analyze_figure(races: list[Race], label: str, ys: int, ye: int, min_n: int) -> dict:
    """1図表のエッジベット診断＋IS内CV（stage_c の機構を再利用）。"""
    from backtest.stage_c_edge_bet import fit_beta

    logger.info(f"\n########## 図表: {label} ##########")
    cov = _coverage(races, ys, ye)
    logger.info(
        f"  被覆: 賭け可能 {cov['bettable_horses']:,}/{cov['total_horses']:,}頭 "
        f"({cov['coverage'] * 100:.1f}%) / 賭け可能レース {cov['bettable_races']:,}"
    )
    sanity = flat_top1(races, ys, ye)
    logger.info(
        f"  サニティ 図表flat-top1単勝: N={sanity['n']:,} / "
        f"的中 {sanity['hit'] * 100:.1f}% / ROI {sanity['roi'] * 100:.1f}%"
    )
    beta_is = fit_beta(races, ys, ye)
    diagnostic = run_diagnostic(races, beta_is)
    cv_all = run_cv(races, min_n, "all")
    cv_best = run_cv(races, min_n, "best")
    return {
        "label": label,
        "coverage": cov,
        "sanity_flat_top1": sanity,
        "beta_is": beta_is,
        "diagnostic": diagnostic,
        "cv_all": cv_all,
        "cv_best": cv_best,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Direction A M3c: 一般戦スピード エッジベット運用（IS限定・OOS封印）"
    )
    ap.add_argument(
        "--min-n", type=int, default=30, help="τ採用に必要な学習区間ベット数の下限"
    )
    ap.add_argument(
        "--smoke", action="store_true", help="小窓で実行可否のみ確認（本番扱いしない）"
    )
    args = ap.parse_args()

    scan_start = SMOKE_SCAN_START if args.smoke else SCAN_START
    ana_end = SMOKE_ANA_END if args.smoke else IS_END
    if ana_end > IS_END:
        logger.error(f"OOS封印違反: ana_end={ana_end} > IS_END={IS_END}")
        sys.exit(1)
    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        logger.info("レース条件をロード中...")
        race_info = load_race_info(conn, scan_start, ana_end)
        logger.info(f"jvd_race_uma を走査中（{scan_start}-{ana_end}・平地戦）...")
        runs = load_runs(conn, race_info, scan_start, ana_end)
    finally:
        conn.close()
    logger.info(f"  平地戦 出走行: {len(runs):,}")

    assign_figures(runs, MIN_DAY_RUNS)
    assign_horse_features(runs)
    a = build_arrays(runs, ana_end)

    figures = []
    for fkey, label in FIGURES:
        races = build_speed_races(a, fkey)
        figures.append(
            {
                "figure": fkey,
                **analyze_figure(races, label, IS_START, IS_END, args.min_n),
            }
        )

    if args.smoke:
        logger.info("\nスモーク完了（結果の良し悪しは判断材料にしない）。")
        return
    out = {
        "milestone": "M3c",
        "is_period": [IS_START, IS_END],
        "min_n": args.min_n,
        "figures": figures,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"\nレポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

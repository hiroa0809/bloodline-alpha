"""#B3 頑健性検証: マルチシード安定性 + IS内ウォークフォワードCV。

Stage 1（optimize_weights.py）が出した「IS ROI 96.83%」が本物か過学習かを、
OOS を一切使わずに IS の中だけで見極める。OOS は #B4 用の一度きりの弾なので、
過学習の検出に消費しない（CLAUDE.md「金庫ルール」）。

2つの検証:
  ① マルチシード — 同じ最適化を複数の乱数シードで回し、最良重み・ROI の安定性を見る。
     シードを変えても A→低・B→高 等の重みと ROI が再現するなら本物。バラつくなら
     目的関数が平坦＝重みに意味が無くノイズを拾っている疑い（診断ツール）。
  ② IS内ウォークフォワードCV — IS をさらに学習/検証に分け、学習区間で最適化した重みを
     未見の検証区間で採点する。学習ROIと検証ROIの差が過学習の量。検証ROIの平均が
     「過学習補正後の本当の実力見積もり」になり、OOS を撃つ前の事前検査になる。

使い方:
    python backend/backtest/robustness.py                          # 既定（5シード・各800試行）
    python backend/backtest/robustness.py --seeds 3 --n-trials 300 # 軽め
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from backtest.optimize_weights import (  # noqa: E402
    CATEGORY_SUBS,
    IS_END,
    IS_START,
    expand_weights,
    optimize_range,
)
from backtest.run_backtest import evaluate, load_races  # noqa: E402

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "robustness_report.json"

# IS内ウォークフォワードCV分割（学習開始, 学習終了, 検証開始, 検証終了）。
# 学習はアンカー型（1993起点）で伸ばし、その直後の未見スライスを検証に使う。
CV_FOLDS = [
    (1993, 2005, 2006, 2008),
    (1993, 2008, 2009, 2011),
    (1993, 2011, 2012, 2013),
]

# 頑健性検証は Stage 1 の勝者 TPE で行う。
METHOD = "TPE"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def run_multiseed(races: dict, seeds: list[int], n_trials: int) -> dict:
    """複数シードで IS 全体を最適化し、ROI と各カテゴリ重みの安定性を集計する。"""
    logger.info(
        f"=== ① マルチシード安定性（IS {IS_START}-{IS_END}・{len(seeds)}シード） ==="
    )
    rows = []
    for seed in seeds:
        t0 = time.time()
        res = optimize_range(races, IS_START, IS_END, METHOD, n_trials, seed)
        rows.append(res)
        logger.info(
            f"  seed={seed}: IS ROI {res['value'] * 100:.2f}% / "
            + ", ".join(f"{c}={res['cat_weights'][c]:.3f}" for c in CATEGORY_SUBS)
            + f"  wr_blend={res['wr_blend']:.3f} ({time.time() - t0:.0f}秒)"
        )

    rois = [r["value"] for r in rows]
    stats = {
        "roi_mean": statistics.mean(rois),
        "roi_std": statistics.pstdev(rois),
        "roi_min": min(rois),
        "roi_max": max(rois),
        "cat_weight_std": {
            c: statistics.pstdev([r["cat_weights"][c] for r in rows])
            for c in CATEGORY_SUBS
        },
    }
    logger.info(
        f"  → IS ROI 平均 {stats['roi_mean'] * 100:.2f}% "
        f"±{stats['roi_std'] * 100:.2f}（{stats['roi_min'] * 100:.2f}〜{stats['roi_max'] * 100:.2f}%）"
    )
    logger.info(
        "  → カテゴリ重みのばらつき(σ): "
        + ", ".join(f"{c}={stats['cat_weight_std'][c]:.3f}" for c in CATEGORY_SUBS)
    )
    return {"runs": rows, "stats": stats}


def run_walkforward_cv(races: dict, seeds: list[int], n_trials: int) -> dict:
    """IS内ウォークフォワードCV。学習で最適化→未見の検証で採点し、過学習量を測る。"""
    logger.info(
        f"=== ② IS内ウォークフォワードCV（{len(CV_FOLDS)}フォールド×{len(seeds)}シード） ==="
    )
    fold_results = []
    for tr_s, tr_e, va_s, va_e in CV_FOLDS:
        train_rois, valid_rois = [], []
        for seed in seeds:
            res = optimize_range(races, tr_s, tr_e, METHOD, n_trials, seed)
            weights = expand_weights(res["cat_weights"])
            valid = evaluate(races, va_s, va_e, weights, res["wr_blend"])
            train_rois.append(res["value"])
            valid_rois.append(valid["roi"])
        tr_mean = statistics.mean(train_rois)
        va_mean = statistics.mean(valid_rois)
        fold_results.append(
            {
                "train": [tr_s, tr_e],
                "valid": [va_s, va_e],
                "train_roi_mean": tr_mean,
                "valid_roi_mean": va_mean,
                "gap": tr_mean - va_mean,
            }
        )
        logger.info(
            f"  学習{tr_s}-{tr_e}→検証{va_s}-{va_e}: "
            f"学習ROI {tr_mean * 100:.2f}% / 検証ROI {va_mean * 100:.2f}% "
            f"(過学習ギャップ {(tr_mean - va_mean) * 100:+.2f}pt)"
        )

    valid_overall = statistics.mean([f["valid_roi_mean"] for f in fold_results])
    gap_overall = statistics.mean([f["gap"] for f in fold_results])
    logger.info(
        f"  → 検証ROI平均（過学習補正後の実力見積もり）: {valid_overall * 100:.2f}%"
    )
    logger.info(f"  → 平均過学習ギャップ: {gap_overall * 100:+.2f}pt")
    return {
        "folds": fold_results,
        "valid_roi_overall": valid_overall,
        "gap_overall": gap_overall,
    }


def main() -> None:
    """マルチシード安定性とIS内CVを実行し、頑健性レポートを保存する（OOS封印）。"""
    ap = argparse.ArgumentParser(description="#B3 頑健性検証（マルチシード+IS内CV）")
    ap.add_argument("--seeds", type=int, default=5, help="シード本数（42から連番）")
    ap.add_argument("--n-trials", type=int, default=800, help="1最適化あたりの試行数")
    args = ap.parse_args()
    seeds = list(range(42, 42 + args.seeds))

    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    try:
        races = load_races(conn)
    finally:
        conn.close()

    start = time.time()
    multiseed = run_multiseed(races, seeds, args.n_trials)
    cv = run_walkforward_cv(races, seeds, args.n_trials)

    out = {
        "method": METHOD,
        "seeds": seeds,
        "n_trials": args.n_trials,
        "is_period": [IS_START, IS_END],
        "multiseed": multiseed,
        "cross_validation": cv,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("=== 頑健性サマリー ===")
    logger.info(
        f"マルチシード IS ROI: {multiseed['stats']['roi_mean'] * 100:.2f}% "
        f"±{multiseed['stats']['roi_std'] * 100:.2f}"
    )
    logger.info(
        f"IS内CV 検証ROI（実力見積もり）: {cv['valid_roi_overall'] * 100:.2f}% "
        f"/ 過学習ギャップ {cv['gap_overall'] * 100:+.2f}pt"
    )
    logger.info(f"レポート保存: {OUT_PATH} ({time.time() - start:.0f}秒)")


if __name__ == "__main__":
    main()

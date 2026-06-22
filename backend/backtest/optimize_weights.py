"""配点最適化（Phase 1-B2 / #B3 Stage 1: カテゴリ単位）。

#B2 の評価装置（run_backtest.evaluate）を目的関数として、IS 区間（1993-2013）の
ROI（スコア1位・単勝）を最大化する重みを Optuna で探索する。GA（NSGA-II）と
ベイズ（TPE）の両方を回し、IS で良かった方を採用する（CLAUDE.md「最適化アルゴリズム」）。

設計の要点（CLAUDE.md「バックテスト方法論」/ pre-registration）:
  - スケール不変性: 総合スコアは重みに線形で、賭けは各レースの argmax。全重みを定数倍
    しても順位は不変＝効くのは相対比のみ。次元から全体スケールを1本落とせる。
  - 段階的最適化: Stage 1 はカテゴリ重み（A/B/C/E）＋勝率/ROIブレンド比のみを動かし、
    カテゴリ内のサブ項目比はライブ既定に固定（実効4次元・過学習しにくい）。サブ項目の
    展開は Stage 2 へ。
  - 金庫ルール: 最適化・手法の優劣判定は IS だけで完結。OOS-1〜3 は本スクリプトで
    一切評価しない（封印）。OOS 判定は #B4 で一度だけ。

なお最適化コア（make_objective / optimize_range / expand_weights）は任意の年範囲を
受け取れるよう汎用化してあり、#B3 頑健性検証（robustness.py）から再利用される。

使い方:
    python backend/backtest/optimize_weights.py                 # GA/TPE 各1000試行
    python backend/backtest/optimize_weights.py --n-trials 50   # 動作確認用に少なく
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import optuna

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from backtest.run_backtest import (  # noqa: E402
    DEFAULT_WEIGHTS,
    DEFAULT_WR_BLEND,
    evaluate,
    load_races,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "best_weights.json"

# IS 区間（CLAUDE.md「バックテスト方法論」）。OOS はここで触らない（金庫ルール）。
IS_START, IS_END = 1993, 2013

# カテゴリ → サブ項目。Stage 1 はカテゴリ重みのみ動かし、内部比は既定固定。
CATEGORY_SUBS = {
    "A": ["A1", "A2", "A3", "A4", "A5"],
    "B": ["B1", "B2", "B3", "B4"],
    "C": ["C1", "C2", "C3"],
    "E": ["E1", "E2"],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
optuna.logging.set_verbosity(optuna.logging.WARNING)


def expand_weights(cat_weights: dict) -> dict:
    """カテゴリ重み → 14サブ項目重み（カテゴリ内比はライブ既定を維持）。"""
    w: dict = {}
    for cat, subs in CATEGORY_SUBS.items():
        base_total = sum(DEFAULT_WEIGHTS[s] for s in subs)
        for s in subs:
            w[s] = cat_weights[cat] * DEFAULT_WEIGHTS[s] / base_total
    return w


def make_objective(races: dict, y_start: int, y_end: int):
    """指定年範囲の ROI を返す Optuna 目的関数を生成する（races はクロージャで共有）。"""

    def objective(trial: optuna.Trial) -> float:
        cat_weights = {
            c: trial.suggest_float(f"W_{c}", 0.0, 1.0) for c in CATEGORY_SUBS
        }
        wr_blend = trial.suggest_float("wr_blend", 0.0, 1.0)
        weights = expand_weights(cat_weights)
        return evaluate(races, y_start, y_end, weights, wr_blend)["roi"]

    return objective


def _make_sampler(method: str, seed: int) -> optuna.samplers.BaseSampler:
    """手法名（"GA"/"TPE"）→ Optuna サンプラー。"""
    if method == "GA":
        return optuna.samplers.NSGAIISampler(seed=seed)
    if method == "TPE":
        return optuna.samplers.TPESampler(seed=seed)
    raise ValueError(f"未知の手法: {method}")


def optimize_range(
    races: dict,
    y_start: int,
    y_end: int,
    method: str,
    n_trials: int,
    seed: int,
) -> dict:
    """指定年範囲で ROI を最大化し、最良の重み・ブレンド比・ROI を返す。

    返却: {"value": IS ROI, "cat_weights": {...}, "wr_blend": float}
    """
    # 有効レースが0件のレンジは ROI=0.0 で「成功」扱いになり任意パラメータが
    # 最良値として保存され下流へ伝播する。明示的に失敗させる（fail-fast）。
    probe = evaluate(races, y_start, y_end, DEFAULT_WEIGHTS, DEFAULT_WR_BLEND)
    if probe["n"] == 0:
        raise ValueError(
            f"指定期間 {y_start}-{y_end} に最適化対象の有効レースがありません。"
        )

    study = optuna.create_study(
        direction="maximize", sampler=_make_sampler(method, seed)
    )
    study.optimize(
        make_objective(races, y_start, y_end),
        n_trials=n_trials,
        show_progress_bar=False,
    )
    return {
        "value": study.best_value,
        "cat_weights": {c: study.best_params[f"W_{c}"] for c in CATEGORY_SUBS},
        "wr_blend": study.best_params["wr_blend"],
    }


def main() -> None:
    """IS 上で GA/TPE を回し、勝者の最適重みを JSON 保存する（OOS は封印）。"""
    ap = argparse.ArgumentParser(description="配点最適化 Stage 1（#B3）")
    ap.add_argument("--n-trials", type=int, default=1000, help="各手法の試行数")
    ap.add_argument("--seed", type=int, default=42, help="サンプラー乱数シード")
    args = ap.parse_args()
    if args.n_trials < 1:
        ap.error("--n-trials は 1 以上を指定してください")

    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    try:
        races = load_races(conn)
    finally:
        conn.close()

    baseline = evaluate(races, IS_START, IS_END, DEFAULT_WEIGHTS, DEFAULT_WR_BLEND)
    logger.info(f"=== #B3 Stage 1 配点最適化（IS {IS_START}-{IS_END}・OOS封印） ===")
    logger.info(
        f"ベースライン（ライブ既定重み）: IS ROI {baseline['roi'] * 100:.2f}% "
        f"/ 的中率 {baseline['hit'] * 100:.2f}%"
    )

    results: dict = {}
    for method in ("GA", "TPE"):
        t0 = time.time()
        res = optimize_range(races, IS_START, IS_END, method, args.n_trials, args.seed)
        results[method] = res
        logger.info(
            f"  {method}: best IS ROI {res['value'] * 100:.2f}% "
            f"({args.n_trials}試行 / {time.time() - t0:.0f}秒)"
        )

    # 勝者は IS ROI で選ぶ（金庫ルール: OOS を見て選ばない）
    winner_name = max(results, key=lambda k: results[k]["value"])
    winner = results[winner_name]
    cat_weights = winner["cat_weights"]
    wr_blend = winner["wr_blend"]
    weights = expand_weights(cat_weights)

    out = {
        "stage": 1,
        "method": winner_name,
        "is_period": [IS_START, IS_END],
        "baseline_is_roi": baseline["roi"],
        "best_is_roi": winner["value"],
        "wr_blend": wr_blend,
        "category_weights": cat_weights,
        "weights": weights,
        "n_trials": args.n_trials,
        "seed": args.seed,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("=== 結果 ===")
    logger.info(
        f"採用手法: {winner_name} / IS ROI "
        f"{baseline['roi'] * 100:.2f}% → {winner['value'] * 100:.2f}%"
    )
    logger.info(f"勝率/ROIブレンド比（勝率側）: {wr_blend:.3f}")
    logger.info(
        "カテゴリ相対重み: "
        + ", ".join(f"{c}={cat_weights[c]:.3f}" for c in CATEGORY_SUBS)
    )
    logger.info(f"最適重みを保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

"""#B5 Stage B: ロバストROI最適化（目的関数の頑健化）。

#B3 で「IS 単一ブロック(1993-2013)の素のROIを最大化」する重み最適化は過学習と判明
（IS96%→未見実力74%＜手置きベースライン85%）。原因は ①目的関数が学習期間に偶発した
少数の高配当ロングショットに丸暗記で合うこと ②動かすツマミ（14次元）が多くノイズを拾う
こと。本スクリプトは過学習を「目的関数」と「次元」の2面から構造的に抑える（賭けは従来
どおりスコア1位単勝のまま、重みの決め方だけを頑健化）。

2つの仕掛け:
  ① 時期間の一貫性を要求するロバスト目的: IS を連続3ブロックに等分し、候補重みを3ブロック
     全部で採点 → 目的 = 加重平均(block_rois) - 加重std(block_rois) を最大化（重みは各
     ブロックの有効レース数）。ある時期だけ大穴で稼ぐ重みは std ペナルティで負け、どの時期
     でも安定して良い重みが勝つ（λ=1固定で追加ツマミを作らない）。
  ② 次元圧縮: #B3 で「効く安定信号は B（条件別の父・母父成績）のみ。A/C/E はノイズ拾い」と
     判明。A/C/E のカテゴリ相対重みは DEFAULT 固定（0.65/0.10/0.05）、探索は W_B と
     wr_blend の2次元のみ。2次元は構造的に過学習の自由度がほぼ無い。

GA(NSGA-II) と TPE 両方を回し、ロバスト目的値が良い方を採用（判定は IS 内で完結＝金庫OK）。

金庫ルール（CLAUDE.md「バックテスト方法論」）:
  - 評価・手法選択は IS(1993-2013)限定。OOS-1〜3 は本スクリプトで一切評価しない（封印）。
    #B4 が唯一の OOS 弾。
  - ブロック分割・目的関数・探索次元は事前登録（OOS を見て後から変えない）。

使い方:
    python backend/backtest/optimize_robust.py                 # GA/TPE 各1000試行
    python backend/backtest/optimize_robust.py --n-trials 50   # 動作確認用に少なく
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
from backtest.optimize_weights import _make_sampler, expand_weights  # noqa: E402
from backtest.run_backtest import (  # noqa: E402
    DEFAULT_WEIGHTS,
    DEFAULT_WR_BLEND,
    count_bettable_races,
    evaluate,
    load_races,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "robust_weights.json"

# IS 区間（CLAUDE.md「バックテスト方法論」）。OOS はここで触らない（金庫ルール）。
IS_START, IS_END = 1993, 2013

# 時期間の一貫性を測る連続3ブロック（IS を等分割・事前登録）。
IS_BLOCKS = [(1993, 1999), (2000, 2006), (2007, 2013)]

# A/C/E のカテゴリ相対重みは DEFAULT に固定（#B3: B 以外はノイズ拾い）。B のみ探索。
FIXED_CATS = {"A": 0.65, "C": 0.10, "E": 0.05}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
optuna.logging.set_verbosity(optuna.logging.WARNING)


def build_weights(w_b: float) -> dict:
    """B強度 W_B → 14サブ項目重み。A/C/E は DEFAULT 固定、B 内のサブ比も DEFAULT 維持。"""
    return expand_weights({**FIXED_CATS, "B": w_b})


def block_sample_counts(races: dict) -> list[int]:
    """各 IS ブロックの有効レース数（重み非依存・count_bettable_races）。目的の加重に使う。"""
    return [count_bettable_races(races, s, e) for s, e in IS_BLOCKS]


def block_rois(races: dict, weights: dict, wr_blend: float) -> list[float]:
    """各 IS ブロックのスコア1位単勝 ROI を返す。"""
    return [evaluate(races, s, e, weights, wr_blend)["roi"] for s, e in IS_BLOCKS]


def weighted_mean_std(rois: list[float], ns: list[int]) -> tuple[float, float]:
    """ブロックROIの有効レース数による加重平均と加重標準偏差を返す。

    ブロックごとに有効レース数が異なる（早期ほど多い）ため等重みだと標本数の少ない
    ブロックの極端値が目的を過度に支配する。robustness.py の加重平均規約に合わせ、
    平均・分散とも同一の重み系（count_bettable_races）で算出する。
    """
    total = sum(ns)
    mean = sum(r * n for r, n in zip(rois, ns)) / total
    var = sum(n * (r - mean) ** 2 for r, n in zip(rois, ns)) / total
    return mean, var**0.5


def robust_objective_value(rois: list[float], ns: list[int]) -> float:
    """ロバスト目的 = 加重平均(block_rois) - 加重std(block_rois)。高くかつ一貫した重みを選ぶ。"""
    mean, std = weighted_mean_std(rois, ns)
    return mean - std


def make_objective(races: dict, block_ns: list[int]):
    """W_B / wr_blend の2次元を探索し、ロバスト目的値を返す Optuna 目的関数。"""

    def objective(trial: optuna.Trial) -> float:
        w_b = trial.suggest_float("W_B", 0.0, 1.0)
        wr_blend = trial.suggest_float("wr_blend", 0.0, 1.0)
        weights = build_weights(w_b)
        return robust_objective_value(block_rois(races, weights, wr_blend), block_ns)

    return objective


def optimize_robust(
    races: dict, block_ns: list[int], method: str, n_trials: int, seed: int
) -> dict:
    """指定手法でロバスト目的を最大化し、最良の W_B / wr_blend / 目的値を返す。"""
    study = optuna.create_study(
        direction="maximize", sampler=_make_sampler(method, seed)
    )
    study.optimize(
        make_objective(races, block_ns), n_trials=n_trials, show_progress_bar=False
    )
    return {
        "value": study.best_value,
        "W_B": study.best_params["W_B"],
        "wr_blend": study.best_params["wr_blend"],
    }


def summarize(races: dict, weights: dict, wr_blend: float, block_ns: list[int]) -> dict:
    """重みの full-IS ROI / per-block ROI / ロバスト目的値をまとめる（レポート用）。"""
    rois = block_rois(races, weights, wr_blend)
    full = evaluate(races, IS_START, IS_END, weights, wr_blend)
    mean, std = weighted_mean_std(rois, block_ns)
    return {
        "full_is_roi": full["roi"],
        "full_is_hit": full["hit"],
        "block_rois": rois,
        "block_std": std,
        "robust_obj": mean - std,
    }


def _log_summary(label: str, s: dict) -> None:
    """サマリーを1行＋ブロック内訳で出力。"""
    blocks = " / ".join(
        f"{ys}-{ye}:{r * 100:.1f}%" for (ys, ye), r in zip(IS_BLOCKS, s["block_rois"])
    )
    logger.info(
        f"  {label}: full-IS ROI {s['full_is_roi'] * 100:.2f}% "
        f"(的中 {s['full_is_hit'] * 100:.1f}%) / ロバスト目的 {s['robust_obj'] * 100:.2f} "
        f"/ ブロックstd {s['block_std'] * 100:.2f}pt"
    )
    logger.info(f"      ブロック別ROI: {blocks}")


def main() -> None:
    """IS 上で GA/TPE のロバスト最適化を回し、最良重みを JSON 保存する（OOS は封印）。"""
    ap = argparse.ArgumentParser(description="#B5 Stage B ロバストROI最適化")
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

    # 各ブロックの有効レース数（重み非依存）を一度だけ計算し、目的の加重に使い回す。
    # 有効レース0のブロックがあると ROI=0 が紛れ込み目的を歪めるため fail-fast。
    block_ns = block_sample_counts(races)
    for (s, e), n in zip(IS_BLOCKS, block_ns):
        if n == 0:
            logger.error(f"ブロック {s}-{e} に有効レースがありません。")
            sys.exit(1)

    logger.info(
        f"=== #B5 Stage B ロバストROI最適化（IS {IS_START}-{IS_END}・OOS封印） ==="
    )
    baseline = summarize(races, DEFAULT_WEIGHTS, DEFAULT_WR_BLEND, block_ns)
    _log_summary("ベースライン（ライブ既定重み）", baseline)

    results: dict = {}
    for method in ("GA", "TPE"):
        t0 = time.time()
        res = optimize_robust(races, block_ns, method, args.n_trials, args.seed)
        results[method] = res
        logger.info(
            f"  {method}: best ロバスト目的 {res['value'] * 100:.2f} "
            f"(W_B={res['W_B']:.3f} / wr_blend={res['wr_blend']:.3f} / "
            f"{args.n_trials}試行 / {time.time() - t0:.0f}秒)"
        )

    # 勝者はロバスト目的値で選ぶ（IS 内で完結＝金庫ルール）。
    winner_name = max(results, key=lambda k: results[k]["value"])
    winner = results[winner_name]
    weights = build_weights(winner["W_B"])
    best = summarize(races, weights, winner["wr_blend"], block_ns)

    logger.info("=== 結果 ===")
    logger.info(f"採用手法: {winner_name}")
    _log_summary("最適（ロバスト）", best)
    delta = (best["full_is_roi"] - baseline["full_is_roi"]) * 100
    logger.info(
        f"  → ベースライン比: full-IS ROI {delta:+.2f}pt / "
        f"ブロックstd {(best['block_std'] - baseline['block_std']) * 100:+.2f}pt"
        "（負＝より一貫）"
    )

    out = {
        "stage": "B5-B",
        "method": winner_name,
        "is_period": [IS_START, IS_END],
        "is_blocks": IS_BLOCKS,
        "block_sample_counts": block_ns,
        "objective": "weighted_mean(block_roi) - weighted_std(block_roi) [race-count weighted]",
        "search_dims": ["W_B", "wr_blend"],
        "fixed_category_weights": FIXED_CATS,
        "best": {
            "W_B": winner["W_B"],
            "wr_blend": winner["wr_blend"],
            **best,
            "weights": weights,
        },
        "baseline": {
            "wr_blend": DEFAULT_WR_BLEND,
            **baseline,
            "weights": DEFAULT_WEIGHTS,
        },
        "n_trials": args.n_trials,
        "seed": args.seed,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"最適重みを保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

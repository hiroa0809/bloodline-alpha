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

再開機能（ユニット単位チェックポイント）:
  1最適化（マルチシードの各シード / CVの各 fold×seed）が約15分かかり全体で数時間に
  なるため、各ユニット完了ごとに robustness_checkpoint.json へ進捗を保存する。途中で
  止まっても、同一パラメータ（seeds/n_trials/method）で再実行すれば完了済みユニットを
  スキップして続きから再開する（最大ロスは進行中の1ユニット）。全完了でチェックポイントは
  削除し、最終レポート robustness_report.json を出力する。

使い方:
    python backend/backtest/robustness.py                          # 既定（5シード・各800試行）
    python backend/backtest/robustness.py --seeds 3 --n-trials 300 # 軽め
    python backend/backtest/robustness.py --fresh                  # チェックポイント無視で最初から
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
from backtest.run_backtest import (  # noqa: E402
    count_bettable_races,
    evaluate,
    load_races,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "robustness_report.json"
CHECKPOINT_PATH = _BACKEND_DIR / "backtest" / "robustness_checkpoint.json"

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


# ============================================================
# チェックポイント（ユニット単位の再開）
# ============================================================


def load_checkpoint(params: dict, fresh: bool) -> dict:
    """チェックポイントを読み込む。パラメータ不一致や --fresh なら新規で開始する。"""
    empty = {"params": params, "multiseed_runs": {}, "cv_units": {}}
    if fresh or not CHECKPOINT_PATH.exists():
        return empty
    try:
        ckpt = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("チェックポイント読込に失敗。新規で開始します。")
        return empty
    if ckpt.get("params") != params:
        logger.warning(
            "チェックポイントのパラメータが現在と不一致（seeds/n_trials/method）。"
            "新規で開始します（古い進捗は無視）。"
        )
        return empty
    done_ms = len(ckpt.get("multiseed_runs", {}))
    done_cv = len(ckpt.get("cv_units", {}))
    logger.info(
        f"チェックポイントから再開: マルチシード {done_ms} / CVユニット {done_cv} 済み"
    )
    return ckpt


def save_checkpoint(ckpt: dict) -> None:
    """チェックポイントを書き出す（各ユニット完了ごとに呼ぶ）。"""
    CHECKPOINT_PATH.write_text(
        json.dumps(ckpt, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ============================================================
# ① マルチシード安定性
# ============================================================


def run_multiseed(races: dict, seeds: list[int], n_trials: int, ckpt: dict) -> dict:
    """複数シードで IS 全体を最適化し、ROI と各カテゴリ重みの安定性を集計する。"""
    logger.info(
        f"=== ① マルチシード安定性（IS {IS_START}-{IS_END}・{len(seeds)}シード） ==="
    )
    runs = ckpt["multiseed_runs"]
    for seed in seeds:
        key = str(seed)
        if key in runs:
            logger.info(f"  seed={seed}: スキップ（チェックポイント済み）")
            continue
        t0 = time.time()
        res = optimize_range(races, IS_START, IS_END, METHOD, n_trials, seed)
        runs[key] = res
        save_checkpoint(ckpt)
        logger.info(
            f"  seed={seed}: IS ROI {res['value'] * 100:.2f}% / "
            + ", ".join(f"{c}={res['cat_weights'][c]:.3f}" for c in CATEGORY_SUBS)
            + f"  wr_blend={res['wr_blend']:.3f} ({time.time() - t0:.0f}秒)"
        )

    rows = [runs[str(seed)] for seed in seeds]
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


# ============================================================
# ② IS内ウォークフォワードCV
# ============================================================


def run_walkforward_cv(
    races: dict, seeds: list[int], n_trials: int, ckpt: dict
) -> dict:
    """IS内ウォークフォワードCV。学習で最適化→未見の検証で採点し、過学習量を測る。"""
    logger.info(
        f"=== ② IS内ウォークフォワードCV（{len(CV_FOLDS)}フォールド×{len(seeds)}シード） ==="
    )
    units = ckpt["cv_units"]
    fold_results = []
    for tr_s, tr_e, va_s, va_e in CV_FOLDS:
        train_rois, valid_rois = [], []
        for seed in seeds:
            key = f"{tr_s}-{tr_e}|{va_s}-{va_e}|{seed}"
            if key in units:
                u = units[key]
            else:
                res = optimize_range(races, tr_s, tr_e, METHOD, n_trials, seed)
                weights = expand_weights(res["cat_weights"])
                valid = evaluate(races, va_s, va_e, weights, res["wr_blend"])
                u = {"train_roi": res["value"], "valid_roi": valid["roi"]}
                units[key] = u
                save_checkpoint(ckpt)
            train_rois.append(u["train_roi"])
            valid_rois.append(u["valid_roi"])
        tr_mean = statistics.mean(train_rois)
        va_mean = statistics.mean(valid_rois)
        # レース数（重み非依存）。fold 間の加重平均に使う。
        # evaluate の n は選択馬のオッズ有無に依存し重み依存になるため使わない。
        train_n = count_bettable_races(races, tr_s, tr_e)
        valid_n = count_bettable_races(races, va_s, va_e)
        fold_results.append(
            {
                "train": [tr_s, tr_e],
                "valid": [va_s, va_e],
                "train_roi_mean": tr_mean,
                "valid_roi_mean": va_mean,
                "gap": tr_mean - va_mean,
                "train_n": train_n,
                "valid_n": valid_n,
            }
        )
        logger.info(
            f"  学習{tr_s}-{tr_e}→検証{va_s}-{va_e}: "
            f"学習ROI {tr_mean * 100:.2f}% / 検証ROI {va_mean * 100:.2f}% "
            f"(過学習ギャップ {(tr_mean - va_mean) * 100:+.2f}pt)"
        )

    # 検証区間の大きさが fold ごとに違う（3/3/2年）ため、単純平均でなくレース数で
    # 加重平均する。これで検証ROI（実力見積もり）の歪みを抑える。
    total_va_n = sum(f["valid_n"] for f in fold_results)
    total_tr_n = sum(f["train_n"] for f in fold_results)
    valid_overall = (
        sum(f["valid_roi_mean"] * f["valid_n"] for f in fold_results) / total_va_n
        if total_va_n
        else 0.0
    )
    train_overall = (
        sum(f["train_roi_mean"] * f["train_n"] for f in fold_results) / total_tr_n
        if total_tr_n
        else 0.0
    )
    gap_overall = train_overall - valid_overall
    logger.info(
        f"  → 検証ROI加重平均（過学習補正後の実力見積もり）: {valid_overall * 100:.2f}%"
    )
    logger.info(f"  → 過学習ギャップ（加重）: {gap_overall * 100:+.2f}pt")
    return {
        "folds": fold_results,
        "valid_roi_overall": valid_overall,
        "train_roi_overall": train_overall,
        "gap_overall": gap_overall,
    }


# ============================================================
# メイン
# ============================================================


def main() -> None:
    """マルチシード安定性とIS内CVを実行し、頑健性レポートを保存する（OOS封印）。"""

    def _positive_int(v: str) -> int:
        iv = int(v)
        if iv <= 0:
            raise argparse.ArgumentTypeError("1以上の整数を指定してください")
        return iv

    ap = argparse.ArgumentParser(description="#B3 頑健性検証（マルチシード+IS内CV）")
    ap.add_argument(
        "--seeds", type=_positive_int, default=5, help="シード本数（42から連番）"
    )
    ap.add_argument(
        "--n-trials", type=_positive_int, default=800, help="1最適化あたりの試行数"
    )
    ap.add_argument(
        "--fresh", action="store_true", help="チェックポイントを無視して最初から実行"
    )
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

    params = {"method": METHOD, "seeds": seeds, "n_trials": args.n_trials}
    ckpt = load_checkpoint(params, args.fresh)

    start = time.time()
    multiseed = run_multiseed(races, seeds, args.n_trials, ckpt)
    cv = run_walkforward_cv(races, seeds, args.n_trials, ckpt)

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
    # 全完了したのでチェックポイントは削除（次回はクリーンに開始）。
    CHECKPOINT_PATH.unlink(missing_ok=True)

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

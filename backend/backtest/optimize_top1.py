"""#B5 Top-1 教師あり最適化（ROIでなく Top-1 一致率を最大化）。

これまで重み(#B3)・オッズ帯(Stage A)・確率校正/エッジベット(Stage C) の全レバーが
黒字化に届かなかった（CLAUDE.md「バックテスト方法論」）。本スクリプトはユーザー提案の
別路線：賭け ROI でなく「IS 期間で各レースの最高スコア馬＝実際の1着馬」の一致件数
（Top-1 精度）を直接最大化する 14 次元フル重み最適化。市場追随で当てにいくため「当てる」
ことと「黒字」は別物（Stage C が示した"市場に勝てない"問題を直接は解かない）点に留意。

2つの目的（道A/道B）を両方試す:
  道A direct : 最高スコアが1頭に定まりそれが1着なら一致(1)。同点1位が2頭以上は不一致(0)。
               一致率を直接最大化（ユーザーのゴールに忠実）。全馬同スコアの degenerate 解
               （全重み0で全馬横並び→1着が必ず含まれる水増し）は同点不一致で自動的に潰れる。
  道B loglik : 各レースで1着馬に softmax(β*score) が与える確率の平均対数尤度を最大化
               （Top-1 の滑らかな近似・探索が安定）。検証は道A同様の一致率で採点し同じ土俵で比較。

金庫ルール厳守:
  - 学習・手法/道の優劣判定は IS（1993-2013）限定。OOS-1〜3 は一切評価しない（封印）。
  - サブスコアは #B1 as-of キャッシュ（リーク無し）。14 重みは各 0〜1 で自由探索（総合スコアは
    重みに線形＝賭けは argmax でスケール不変、効くのは相対比のみ。道B のみ温度βを別途持つ）。
  - 採否は IS の一致率でなく IS内ウォークフォワードCV（3分割）の検証一致率で判定する
    （#B3 が IS だけ見て過学習した轍を踏まない）。被覆レース数で加重平均＝過学習補正後の実力。

使い方:
    python backend/backtest/optimize_top1.py                  # 本番（GA/TPE 各1000試行）
    python backend/backtest/optimize_top1.py --n-trials 20    # スモーク（動作確認）
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import optuna

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from backtest.optimize_weights import _make_sampler  # noqa: E402
from backtest.run_backtest import (  # noqa: E402
    DEFAULT_WEIGHTS,
    DEFAULT_WR_BLEND,
    compute_score,
    load_races,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
REPORT_PATH = _BACKEND_DIR / "backtest" / "top1_report.json"
WEIGHTS_PATH = _BACKEND_DIR / "backtest" / "top1_weights.json"

# IS 区間（CLAUDE.md「バックテスト方法論」）。OOS はここで触らない（金庫ルール）。
IS_START, IS_END = 1993, 2013

# IS内ウォークフォワードCV分割（robustness / analyze_odds_bands / stage_c と同一値）。
# (学習開始, 学習終了, 検証開始, 検証終了)
CV_FOLDS = [
    (1993, 2005, 2006, 2008),
    (1993, 2008, 2009, 2011),
    (1993, 2011, 2012, 2013),
]

# 14 サブ項目（ライブ既定の重みキー順）。
SUBS = list(DEFAULT_WEIGHTS.keys())

# 道Bの softmax 温度βの探索範囲（重みが各0〜1で最大スコアが小さいため温度で鋭さを調整）。
BETA_LOW, BETA_HIGH = 0.5, 20.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
optuna.logging.set_verbosity(optuna.logging.WARNING)


def _iter_races(races: dict, year_start: int, year_end: int):
    """指定年範囲（as_of_year）のレース runners を順に返す。"""
    for runners in races.values():
        if year_start <= runners[0]["as_of_year"] <= year_end:
            yield runners


def top1_match_rate(
    races: dict, year_start: int, year_end: int, weights: dict, wr_blend: float
) -> tuple[float, int]:
    """道A: Top-1 一致率と分母レース数を返す。

    各レースで最高スコアの馬を取り、それが「ちょうど1頭」かつ「1着(won==1)」なら一致(1)。
    同点1位が2頭以上のレースは不一致(0)＝賭ける1頭を一意に決められない＝実運用で賭けられない。
    分母は年範囲の全レース（同点レースも分母に残すので全馬同点の水増しは一致率0に潰れる）。
    """
    n = match = 0
    for runners in _iter_races(races, year_start, year_end):
        scores = [compute_score(r, weights, wr_blend) for r in runners]
        mx = max(scores)
        top = [r for r, s in zip(runners, scores) if s >= mx - 1e-12]
        n += 1
        if len(top) == 1 and top[0]["won"] == 1:
            match += 1
    return (match / n if n else 0.0, n)


def top1_loglik(
    races: dict,
    year_start: int,
    year_end: int,
    weights: dict,
    wr_blend: float,
    beta: float,
) -> float:
    """道B: 各レースで1着馬に softmax(β*score) が与える確率の平均対数尤度（最大化対象）。

    1着が複数（同着）のレースは各勝者の対数確率を平均。1着不在のレースは分母から除外。
    """
    total = 0.0
    n = 0
    for runners in _iter_races(races, year_start, year_end):
        scores = [compute_score(r, weights, wr_blend) for r in runners]
        mx = max(scores)
        exps = [math.exp(beta * (s - mx)) for s in scores]  # mx 減算で数値安定化
        denom = sum(exps)
        winners = [e for r, e in zip(runners, exps) if r["won"] == 1]
        if not winners:
            continue
        total += sum(math.log(e / denom) for e in winners) / len(winners)
        n += 1
    return total / n if n else -math.inf


def _suggest_weights(trial: optuna.Trial) -> dict:
    """14 サブ重みを各 0〜1 で提案する。"""
    return {s: trial.suggest_float(s, 0.0, 1.0) for s in SUBS}


def make_objective(races: dict, y_start: int, y_end: int, route: str):
    """route="direct"(道A: 一致率) / "loglik"(道B: 対数尤度) の Optuna 目的関数を生成。"""

    def objective(trial: optuna.Trial) -> float:
        weights = _suggest_weights(trial)
        wr_blend = trial.suggest_float("wr_blend", 0.0, 1.0)
        if route == "direct":
            return top1_match_rate(races, y_start, y_end, weights, wr_blend)[0]
        beta = trial.suggest_float("beta", BETA_LOW, BETA_HIGH)
        return top1_loglik(races, y_start, y_end, weights, wr_blend, beta)

    return objective


def _params_to_weights(params: dict) -> tuple[dict, float, float | None]:
    """study.best_params → (14重み, wr_blend, beta or None)。"""
    weights = {s: params[s] for s in SUBS}
    return weights, params["wr_blend"], params.get("beta")


def optimize_range(
    races: dict,
    y_start: int,
    y_end: int,
    route: str,
    method: str,
    n_trials: int,
    seed: int,
) -> dict:
    """指定年範囲で route の目的を最大化。得た重みを共通指標（道A一致率）でも採点して返す。"""
    study = optuna.create_study(
        direction="maximize", sampler=_make_sampler(method, seed)
    )
    study.optimize(
        make_objective(races, y_start, y_end, route),
        n_trials=n_trials,
        show_progress_bar=False,
    )
    weights, wr_blend, beta = _params_to_weights(study.best_params)
    match_rate, n = top1_match_rate(races, y_start, y_end, weights, wr_blend)
    return {
        "objective_value": study.best_value,
        "match_rate": match_rate,  # 共通指標（道A/道B を同じ土俵で比較）
        "n": n,
        "weights": weights,
        "wr_blend": wr_blend,
        "beta": beta,
    }


def optimize_best(
    races: dict, y_start: int, y_end: int, route: str, n_trials: int, seed: int
) -> dict:
    """GA/TPE を両方回し、学習区間の Top-1 一致率で良い方を採用（金庫: 学習区間で完結）。"""
    cands = {
        m: optimize_range(races, y_start, y_end, route, m, n_trials, seed)
        for m in ("GA", "TPE")
    }
    method = max(cands, key=lambda m: cands[m]["match_rate"])
    res = dict(cands[method])
    res["method"] = method
    return res


def eval_cv(races: dict, route: str, n_trials: int, seed: int) -> dict:
    """IS内ウォークフォワードCV: 各 fold の学習区間で最適化→検証区間で一致率を採点。

    検証一致率を検証レース数で加重平均＝過学習補正後の実力見積もり。
    """
    folds = []
    val_sum = 0.0
    val_n = 0
    for ls, le, vs, ve in CV_FOLDS:
        fit = optimize_best(races, ls, le, route, n_trials, seed)
        val_rate, vn = top1_match_rate(races, vs, ve, fit["weights"], fit["wr_blend"])
        folds.append(
            {
                "train": [ls, le],
                "valid": [vs, ve],
                "method": fit["method"],
                "train_match_rate": fit["match_rate"],
                "valid_match_rate": val_rate,
                "valid_n": vn,
                "overfit_gap": fit["match_rate"] - val_rate,
            }
        )
        val_sum += val_rate * vn
        val_n += vn
        logger.info(
            f"  [{route}] 学習{ls}-{le}→検証{vs}-{ve}: {fit['method']} / "
            f"学習一致 {fit['match_rate'] * 100:.2f}% → 検証一致 {val_rate * 100:.2f}% "
            f"(過学習ギャップ {(fit['match_rate'] - val_rate) * 100:+.2f}pt, N={vn})"
        )
    weighted = val_sum / val_n if val_n else 0.0
    return {
        "folds": folds,
        "weighted_valid_match_rate": weighted,
        "total_valid_n": val_n,
    }


def baseline_rates(races: dict, y_start: int, y_end: int) -> dict:
    """参照ベースライン: 手置き既定重みの Top-1 一致率 / 1番人気の的中率（市場の Top-1）。"""
    base_rate, n = top1_match_rate(
        races, y_start, y_end, DEFAULT_WEIGHTS, DEFAULT_WR_BLEND
    )
    fav_n = fav_win = 0
    for runners in _iter_races(races, y_start, y_end):
        fav = next((r for r in runners if r["ninki"] == 1), None)
        if fav is None:
            continue
        fav_n += 1
        if fav["won"] == 1:
            fav_win += 1
    return {
        "default_weight_match_rate": base_rate,
        "default_weight_n": n,
        "favorite_match_rate": fav_win / fav_n if fav_n else 0.0,
        "favorite_n": fav_n,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Top-1 教師あり最適化（#B5・ROIでなく一致率）"
    )
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

    base = baseline_rates(races, IS_START, IS_END)
    logger.info(f"=== Top-1 教師あり最適化（IS {IS_START}-{IS_END}・OOS封印） ===")
    logger.info(
        f"参照: 既定重み Top-1 一致 {base['default_weight_match_rate'] * 100:.2f}% / "
        f"1番人気 的中 {base['favorite_match_rate'] * 100:.2f}%（市場の Top-1）"
    )

    routes = {}
    for route, label in (("direct", "道A 一致件数"), ("loglik", "道B 対数尤度")):
        logger.info(
            f"--- {label}: full-IS 最適化（GA/TPE 各 {args.n_trials} 試行） ---"
        )
        t0 = time.time()
        full = optimize_best(races, IS_START, IS_END, route, args.n_trials, args.seed)
        logger.info(
            f"  採用 {full['method']} / full-IS Top-1 一致 "
            f"{full['match_rate'] * 100:.2f}% ({time.time() - t0:.0f}秒)"
        )
        logger.info(f"--- {label}: IS内CV（過学習チェック） ---")
        cv = eval_cv(races, route, args.n_trials, args.seed)
        logger.info(
            f"  → 検証(未見)一致率 加重平均 {cv['weighted_valid_match_rate'] * 100:.2f}% "
            f"（実力見積もり・総検証N={cv['total_valid_n']}）"
        )
        routes[route] = {"full_is": full, "cv": cv}

    report = {
        "is_period": [IS_START, IS_END],
        "n_trials": args.n_trials,
        "seed": args.seed,
        "baseline": base,
        "routes": routes,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 採用重みは「CV 検証一致率が高い道」を保存（IS でなく汎化で選ぶ＝金庫ルール）。
    best_route = max(routes, key=lambda r: routes[r]["cv"]["weighted_valid_match_rate"])
    best = routes[best_route]["full_is"]
    WEIGHTS_PATH.write_text(
        json.dumps(
            {
                "route": best_route,
                "method": best["method"],
                "weights": best["weights"],
                "wr_blend": best["wr_blend"],
                "beta": best["beta"],
                "full_is_match_rate": best["match_rate"],
                "cv_valid_match_rate": routes[best_route]["cv"][
                    "weighted_valid_match_rate"
                ],
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(f"レポート保存: {REPORT_PATH}")
    logger.info(
        f"採用重み保存: {WEIGHTS_PATH}（採用道={best_route} "
        f"CV検証一致 {routes[best_route]['cv']['weighted_valid_match_rate'] * 100:.2f}%）"
    )


if __name__ == "__main__":
    main()

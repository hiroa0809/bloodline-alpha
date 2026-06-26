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

高速化・運用:
  - スコア計算は `top1_core`（numpy ベクトル化）。pure-Python の compute_score と全頭一致を
    `test_top1_core.py` のゴールデンテストで検証済み。1 試行 15 万回の関数呼び出しを行列演算
    1 発に畳み込み、20,000 試行を現実的な時間で回す。
  - 進捗出力: 各 study で一定試行ごとに best 値と経過秒をログ。
  - resume: Optuna は in-memory で実行し、完了済み study を top1_checkpoint.json に保存。意図しない
    停止後、同一引数で再実行すると完了済み study はスキップし、未完了 study は先頭から再実行する。

金庫ルール厳守:
  - 学習・手法/道の優劣判定は IS（1993-2013）限定。OOS-1〜3 は一切評価しない（封印）。
  - サブスコアは #B1 as-of キャッシュ（リーク無し）。14 重みは各 0〜1 で自由探索（道Bのみ温度β）。
  - 採否は IS の一致率でなく IS内ウォークフォワードCV（3分割）の検証一致率で判定。

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
from backtest import top1_core as core  # noqa: E402
from backtest.optimize_weights import _make_sampler  # noqa: E402
from backtest.run_backtest import (  # noqa: E402
    DEFAULT_WEIGHTS,
    DEFAULT_WR_BLEND,
    compute_score,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
REPORT_PATH = _BACKEND_DIR / "backtest" / "top1_report.json"
WEIGHTS_PATH = _BACKEND_DIR / "backtest" / "top1_weights.json"
CHECKPOINT_PATH = _BACKEND_DIR / "backtest" / "top1_checkpoint.json"

# IS 区間（CLAUDE.md「バックテスト方法論」）。OOS はここで触らない（金庫ルール）。
IS_START, IS_END = 1993, 2013

# IS内ウォークフォワードCV分割（robustness / analyze_odds_bands / stage_c と同一値）。
CV_FOLDS = [
    (1993, 2005, 2006, 2008),
    (1993, 2008, 2009, 2011),
    (1993, 2011, 2012, 2013),
]

# 14 サブ項目（ライブ既定の重みキー順）。
SUBS = list(DEFAULT_WEIGHTS.keys())

# 道Bの softmax 温度βの探索範囲。
BETA_LOW, BETA_HIGH = 0.5, 20.0

# TPE は履歴からのサンプリングが試行数とともに重くなる（O(n)超）一方、ベイズ最適化として
# 少試行で収束する。GA（線形・多試行向き）と試行数を分け、TPE は上限でキャップして全 study を
# 一晩で完了させる（CLAUDE.md「TPE は各300〜500試行で収束」と整合・収束は実測でも頭打ち）。
TPE_MAX_TRIALS = 5000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
optuna.logging.set_verbosity(optuna.logging.WARNING)


# ============================================================
# pure-Python 実装（ゴールデン照合の「正解」。最適化は numpy core を使う）
# ============================================================
def _iter_races(races: dict, year_start: int, year_end: int):
    for runners in races.values():
        if year_start <= runners[0]["as_of_year"] <= year_end:
            yield runners


def top1_match_rate(
    races: dict, year_start: int, year_end: int, weights: dict, wr_blend: float
) -> tuple[float, int]:
    """道A の pure 実装。最高スコアが1頭でそれが1着なら一致。同点1位2頭以上は不一致。"""
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
    """道B の pure 実装。各レースで1着馬の softmax(β*score) 平均対数尤度。"""
    total = 0.0
    n = 0
    for runners in _iter_races(races, year_start, year_end):
        scores = [compute_score(r, weights, wr_blend) for r in runners]
        mx = max(scores)
        zs = [beta * (s - mx) for s in scores]
        log_denom = math.log(sum(math.exp(z) for z in zs))
        winners = [z for r, z in zip(runners, zs) if r["won"] == 1]
        if not winners:
            continue
        total += sum(z - log_denom for z in winners) / len(winners)
        n += 1
    return total / n if n else -math.inf


# ============================================================
# numpy 最適化コア（高速・resume・進捗）
# ============================================================
def make_objective(arrays: dict, y_start: int, y_end: int, route: str):
    """route="direct"(道A: 一致率) / "loglik"(道B: 対数尤度) の Optuna 目的関数。"""

    def objective(trial: optuna.Trial) -> float:
        weights = {s: trial.suggest_float(s, 0.0, 1.0) for s in SUBS}
        wr_blend = trial.suggest_float("wr_blend", 0.0, 1.0)
        scores = core.score_all(arrays, weights, wr_blend)
        if route == "direct":
            return core.top1_match_rate(arrays, scores, y_start, y_end)[0]
        beta = trial.suggest_float("beta", BETA_LOW, BETA_HIGH)
        return core.top1_loglik(arrays, scores, y_start, y_end, beta)

    return objective


def _params_to_weights(params: dict) -> tuple[dict, float, float | None]:
    weights = {s: params[s] for s in SUBS}
    return weights, params["wr_blend"], params.get("beta")


def _make_progress(label: str, t0: float, every: int):
    def callback(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        done = trial.number + 1
        if done % every == 0:
            logger.info(
                f"    [{label}] {done}試行 best={study.best_value:.4f} "
                f"({time.time() - t0:.0f}s)"
            )

    return callback


def _load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(ckpt: dict) -> None:
    # 保存中の中断で checkpoint が壊れ resume 不能になるのを防ぐ（tmp→replace の atomic write）。
    payload = json.dumps(ckpt, ensure_ascii=False, indent=2)
    tmp = CHECKPOINT_PATH.with_suffix(f"{CHECKPOINT_PATH.suffix}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(CHECKPOINT_PATH)


def optimize_range(
    arrays: dict,
    y_start: int,
    y_end: int,
    route: str,
    scope: str,
    method: str,
    n_trials: int,
    seed: int,
    ckpt: dict,
) -> dict:
    """1 study（route×scope×method）を in-memory で n_trials 回し、最良を返す。

    完了結果は checkpoint に保存し、再実行時に同一 study があれば再計算せず復元する
    （study 単位 resume。numpy の高速化を活かすため storage I/O は使わない）。
    共通指標（道A 一致率）でも採点して返し、道A/道B・GA/TPE を同じ土俵で比較する。
    """
    range_key = f"{y_start}-{y_end}"
    key = f"{route}__{scope}__{range_key}__{method}__s{seed}__n{n_trials}"
    label = f"{route}/{scope}/{range_key}/{method}"
    if key in ckpt:
        logger.info(f"    [{label}] checkpoint から復元（スキップ）")
        return ckpt[key]
    study = optuna.create_study(
        direction="maximize", sampler=_make_sampler(method, seed)
    )
    t0 = time.time()
    study.optimize(
        make_objective(arrays, y_start, y_end, route),
        n_trials=n_trials,
        callbacks=[_make_progress(label, t0, max(1, n_trials // 20))],
    )
    weights, wr_blend, beta = _params_to_weights(study.best_params)
    match_rate, n = core.top1_match_rate(
        arrays, core.score_all(arrays, weights, wr_blend), y_start, y_end
    )
    res = {
        "objective_value": study.best_value,
        "match_rate": match_rate,
        "n": n,
        "weights": weights,
        "wr_blend": wr_blend,
        "beta": beta,
        "method": method,
    }
    ckpt[key] = res
    _save_checkpoint(ckpt)
    logger.info(
        f"    [{label}] 完了 {n_trials}試行 ({time.time() - t0:.0f}s)・checkpoint保存"
    )
    return res


def optimize_best(
    arrays: dict,
    y_start: int,
    y_end: int,
    route: str,
    scope: str,
    n_trials: int,
    seed: int,
    ckpt: dict,
) -> dict:
    """GA/TPE を両方回し、学習区間の Top-1 一致率で良い方を採用（金庫: 学習区間で完結）。

    GA は n_trials、TPE は TPE_MAX_TRIALS 上限（TPE は少試行で収束し多試行は重いだけのため）。
    """
    trials_by_method = {"GA": n_trials, "TPE": min(n_trials, TPE_MAX_TRIALS)}
    cands = {
        m: optimize_range(
            arrays, y_start, y_end, route, scope, m, trials_by_method[m], seed, ckpt
        )
        for m in ("GA", "TPE")
    }
    method = max(cands, key=lambda m: cands[m]["match_rate"])
    return cands[method]


def eval_cv(arrays: dict, route: str, n_trials: int, seed: int, ckpt: dict) -> dict:
    """IS内ウォークフォワードCV: 各 fold の学習区間で最適化→検証区間で一致率を採点。"""
    folds = []
    val_sum = 0.0
    val_n = 0
    for i, (ls, le, vs, ve) in enumerate(CV_FOLDS):
        fit = optimize_best(arrays, ls, le, route, f"fold{i}", n_trials, seed, ckpt)
        val_rate, vn = core.top1_match_rate(
            arrays, core.score_all(arrays, fit["weights"], fit["wr_blend"]), vs, ve
        )
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


def baseline_rates(arrays: dict) -> dict:
    """参照ベースライン: 既定重みの Top-1 一致率 / 1番人気の的中率（市場の Top-1）。"""
    base_rate, n = core.top1_match_rate(
        arrays,
        core.score_all(arrays, DEFAULT_WEIGHTS, DEFAULT_WR_BLEND),
        IS_START,
        IS_END,
    )
    fav_rate, fav_n = core.favorite_match_rate(arrays, IS_START, IS_END)
    return {
        "default_weight_match_rate": base_rate,
        "default_weight_n": n,
        "favorite_match_rate": fav_rate,
        "favorite_n": fav_n,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Top-1 教師あり最適化（#B5・ROIでなく一致率）"
    )
    ap.add_argument("--n-trials", type=int, default=1000, help="各手法の試行数")
    ap.add_argument("--seed", type=int, default=42, help="サンプラー乱数シード")
    ap.add_argument(
        "--target",
        choices=["maiden", "general"],
        default="maiden",
        help="maiden=新馬戦14次元 / general=一般戦14次元+SP（Direction A・OOS封印）",
    )
    args = ap.parse_args()
    if args.n_trials < 1:
        ap.error("--n-trials は 1 以上を指定してください")
    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    # 一般戦は SP（走破直近）次元を加え、別キャッシュ・別出力・OOS封印（year_max）で回す。
    global SUBS, REPORT_PATH, WEIGHTS_PATH, CHECKPOINT_PATH
    table, year_max = core.CACHE_TABLE, None
    if args.target == "general":
        if "SP" not in SUBS:
            SUBS = SUBS + ["SP"]
        table, year_max = "backtest_subscore_cache_general", IS_END
        REPORT_PATH = _BACKEND_DIR / "backtest" / "top1_general_report.json"
        WEIGHTS_PATH = _BACKEND_DIR / "backtest" / "top1_weights_general.json"
        CHECKPOINT_PATH = _BACKEND_DIR / "backtest" / "top1_general_checkpoint.json"

    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    try:
        arrays = core.load_arrays(conn, table=table, year_max=year_max)
    finally:
        conn.close()
    logger.info(
        f"対象（target={args.target}・{len(SUBS)}次元）: "
        f"{arrays['N']:,} 頭 / {arrays['R']:,} レース"
    )

    ckpt = _load_checkpoint()
    if ckpt:
        logger.info(f"checkpoint 復元: {len(ckpt)} study 完了済み（再開）")
    base = baseline_rates(arrays)
    logger.info(f"=== Top-1 教師あり最適化（IS {IS_START}-{IS_END}・OOS封印） ===")
    logger.info(
        f"参照: 既定重み Top-1 一致 {base['default_weight_match_rate'] * 100:.2f}% / "
        f"1番人気 的中 {base['favorite_match_rate'] * 100:.2f}%（市場の Top-1）"
    )

    routes = {}
    for route, rlabel in (("direct", "道A 一致件数"), ("loglik", "道B 対数尤度")):
        logger.info(
            f"--- {rlabel}: full-IS 最適化（GA/TPE 各 {args.n_trials} 試行） ---"
        )
        t0 = time.time()
        full = optimize_best(
            arrays, IS_START, IS_END, route, "fullIS", args.n_trials, args.seed, ckpt
        )
        logger.info(
            f"  採用 {full['method']} / full-IS Top-1 一致 "
            f"{full['match_rate'] * 100:.2f}% ({time.time() - t0:.0f}秒)"
        )
        logger.info(f"--- {rlabel}: IS内CV（過学習チェック） ---")
        cv = eval_cv(arrays, route, args.n_trials, args.seed, ckpt)
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
    CHECKPOINT_PATH.unlink(missing_ok=True)  # 全完了＝checkpoint不要


if __name__ == "__main__":
    main()

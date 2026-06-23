"""#B5 Stage C: スコア確率校正 + エッジベット（選択的ベット）の検証。

これまでの結論（CLAUDE.md「バックテスト方法論」/ project_b3_overfit_finding）:
  - #B2 全レース強制ベットは赤字、#B3 重み最適化は過学習（IS96%→CV74%）、#B5 Stage A
    オッズ帯フィルタも汎化せず。さらに #B5 Stage B ロバスト重みでも full-IS ROI は
    88.5%（<100%）。つまり「全レースでスコア1位を単勝強制」する賭け方は配点をどう
    いじっても黒字化しない。限界は配点ではなく“賭け方”にある。

Stage C のアイデア（黒字化への唯一の現実的ルート）:
  ① 確率校正: 固定スコアを1特徴量とするレース内 conditional logit（softmax 温度β）で
     各馬の勝率 p を推定。β は学習区間の勝者対数尤度を最大化して当てる（1パラメータ＝
     過学習しにくい）。p はレース内で合計1（新馬戦＝1レース1勝に自然）。
  ② エッジ: edge = p × 単勝オッズ − 1。モデルが市場より高く評価する（＝過小評価された）
     馬ほど edge>0 になる。
  ③ 選択的ベット: edge>しきい値τ の馬だけ単勝1単位。全レース強制をやめる＝100%超の鍵。

金庫ルール厳守:
  - 校正β・しきい値τ・診断はすべて IS（1993-2013）限定で決める。OOS-1〜3 は封印。
  - スコアは固定重み（既定: Stage B ロバスト重み robust_weights.json、無ければ DEFAULT）。
    Stage C はスコアの上に乗る「確率校正＋ベット選択層」だけを検証する（重みは再最適化
    しない＝#B3 の過学習を上塗りしない）。
  - サブスコアは #B1 の as-of キャッシュ（リーク無し）。β/τ も学習区間のみで推定＝リーク無し。

2部構成（analyze_odds_bands.py と同じ思想）:
  Part 1 診断 — IS全体で β を当て、予測 edge 別に実 ROI を集計（校正の妥当性＝予測 edge が
    高い群ほど実 ROI が高いか）＋ τ=0 で全 +edge 馬を賭けたときの ROI/被覆。
  Part 2 IS内CV — 学習区間で β・τ を決め、未見の検証区間で採点。検証(選択)ROI を被覆数で
    加重平均＝過学習補正後の実力見積もり。学習との差＝過学習ギャップ。

使い方:
    python backend/backtest/stage_c_edge_bet.py
    python backend/backtest/stage_c_edge_bet.py --min-n 50
"""

from __future__ import annotations

import argparse
import json
import logging
import math
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
    compute_score,
    load_races,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
ROBUST_PATH = _BACKEND_DIR / "backtest" / "robust_weights.json"
OUT_PATH = _BACKEND_DIR / "backtest" / "stage_c_report.json"

# IS（学習区間）。OOS は封印。
IS_START, IS_END = 1993, 2013

# IS内ウォークフォワードCV分割（analyze_odds_bands / robustness と同一値）。
CV_FOLDS = [
    (1993, 2005, 2006, 2008),
    (1993, 2008, 2009, 2011),
    (1993, 2011, 2012, 2013),
]

# エッジしきい値 τ の探索格子（事前登録・OOSを見る前に固定）。
TAU_GRID = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 0.75, 1.0]

# 診断用の予測エッジ区分（下限含む, 上限含まない, ラベル）。
EDGE_BUCKETS = [
    (-float("inf"), 0.0, "edge<0"),
    (0.0, 0.1, "0.0-0.1"),
    (0.1, 0.25, "0.1-0.25"),
    (0.25, 0.5, "0.25-0.5"),
    (0.5, 1.0, "0.5-1.0"),
    (1.0, float("inf"), "1.0+"),
]
EDGE_LABELS = [label for _, _, label in EDGE_BUCKETS]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ---- スコア前計算（重み固定なので一度だけ） -------------------------------


class Race:
    """1レース分の前計算済みデータ（スコア・オッズ・勝敗）。

    scores: 全出走馬のスコア（校正に使う）。
    odds:   各馬の単勝オッズ（None=賭け不可）。
    won:    各馬の勝ち0/1。
    winner: 勝ち馬のインデックス（一意でなければ None＝校正の尤度に使わない）。
    """

    __slots__ = ("year", "scores", "odds", "won", "winner")

    def __init__(self, year, scores, odds, won, winner):
        self.year = year
        self.scores = scores
        self.odds = odds
        self.won = won
        self.winner = winner


def build_races(
    races: dict[str, list[sqlite3.Row]], weights: dict, wr_blend: float
) -> list[Race]:
    """キャッシュ行 → スコア前計算済み Race のリスト（重み固定で一度だけ）。"""
    out: list[Race] = []
    for runners in races.values():
        year = runners[0]["as_of_year"]
        scores = [compute_score(r, weights, wr_blend) for r in runners]
        odds = [
            float(r["tansho_odds"]) if r["tansho_odds"] is not None else None
            for r in runners
        ]
        won = [1 if r["won"] == 1 else 0 for r in runners]
        win_idx = [i for i, w in enumerate(won) if w == 1]
        winner = win_idx[0] if len(win_idx) == 1 else None
        out.append(Race(year, scores, odds, won, winner))
    return out


def _in_range(race: Race, ys: int, ye: int) -> bool:
    return ys <= race.year <= ye


# ---- 確率校正（conditional logit の温度βを勝者尤度MLEで当てる） -----------


def _softmax(scores: list[float], beta: float) -> list[float]:
    """β·score の softmax。レース内で合計1の勝率を返す。"""
    m = max(s * beta for s in scores)
    exps = [math.exp(s * beta - m) for s in scores]
    z = sum(exps)
    return [e / z for e in exps]


def fit_beta(races: list[Race], ys: int, ye: int) -> float:
    """学習区間の勝者対数尤度を最大化する温度βをニュートン法で求める（1次元・凹）。

    LL'(β)  = Σ (score_winner − E_p[score])
    LL''(β) = −Σ Var_p(score)  < 0
    分散ゼロ（全馬同点）のレースは寄与0。全レースで分散ゼロなら β=0（一様）を返す。
    """
    train = [
        r
        for r in races
        if _in_range(r, ys, ye) and r.winner is not None and len(r.scores) >= 2
    ]
    if not train:
        return 0.0
    beta = 0.0
    for _ in range(100):
        grad = 0.0
        hess = 0.0
        for r in train:
            p = _softmax(r.scores, beta)
            mean = sum(pi * s for pi, s in zip(p, r.scores))
            grad += r.scores[r.winner] - mean
            var = sum(pi * (s - mean) ** 2 for pi, s in zip(p, r.scores))
            hess -= var
        if hess == 0.0:  # どのレースも分散ゼロ＝スコアに識別力なし
            return 0.0
        step = grad / hess  # ニュートン: β -= LL'/LL''（hess<0 で上昇方向）
        beta -= step
        if abs(step) < 1e-8:
            break
    return beta


# ---- エッジベットの集計 ----------------------------------------------------


def _metrics(n: int, wins: int, ret: float) -> dict:
    """N・的中率・ROI（ROI=勝ち馬オッズ累積/N、100%=損益分岐）。"""
    return {"n": n, "hit": wins / n if n else 0.0, "roi": ret / n if n else 0.0}


def collect_edge_bets(
    races: list[Race], ys: int, ye: int, beta: float, tau: float, mode: str = "all"
) -> dict:
    """edge>τ の馬を賭けたときの N（被覆ベット数）・的中率・ROI。

    mode="all":  edge>τ の馬を全て賭ける（1レース複数可・大穴に溺れやすい弱ルール）。
    mode="best": 各レースで edge 最大の1頭だけ、その edge>τ なら賭ける（本来の選択的ベット）。
    """
    n = wins = 0
    ret = 0.0
    for r in races:
        if not _in_range(r, ys, ye):
            continue
        p = _softmax(r.scores, beta)
        if mode == "best":
            # オッズ有りの馬のうち edge 最大の1頭を選ぶ。
            best_i, best_edge = None, -float("inf")
            for i, o in enumerate(r.odds):
                if o is None:
                    continue
                edge = p[i] * o - 1.0
                if edge > best_edge:
                    best_i, best_edge = i, edge
            if best_i is not None and best_edge > tau:
                n += 1
                if r.won[best_i] == 1:
                    wins += 1
                    ret += r.odds[best_i]
        else:
            for i, o in enumerate(r.odds):
                if o is None:
                    continue
                edge = p[i] * o - 1.0
                if edge > tau:
                    n += 1
                    if r.won[i] == 1:
                        wins += 1
                        ret += o
    return _metrics(n, wins, ret)


def aggregate_by_edge(races: list[Race], ys: int, ye: int, beta: float) -> dict:
    """賭け可能な全馬を予測エッジ区分別に集計（校正の妥当性チェック）。"""
    raw = {label: [0, 0, 0.0] for label in EDGE_LABELS}  # [n, wins, ret]
    for r in races:
        if not _in_range(r, ys, ye):
            continue
        p = _softmax(r.scores, beta)
        for i, o in enumerate(r.odds):
            if o is None:
                continue
            edge = p[i] * o - 1.0
            label = _edge_label(edge)
            raw[label][0] += 1
            if r.won[i] == 1:
                raw[label][1] += 1
                raw[label][2] += o
    return {label: _metrics(*raw[label]) for label in EDGE_LABELS}


def _edge_label(edge: float) -> str:
    for lo, hi, label in EDGE_BUCKETS:
        if lo <= edge < hi:
            return label
    return EDGE_LABELS[-1]  # +inf 安全網


def select_tau(
    races: list[Race], ys: int, ye: int, beta: float, min_n: int, mode: str
) -> float:
    """学習区間で N≥min_n を満たす τ のうち ROI 最大の τ を選ぶ（無ければ τ=0）。"""
    best_tau, best_roi = 0.0, -1.0
    for tau in TAU_GRID:
        m = collect_edge_bets(races, ys, ye, beta, tau, mode)
        if m["n"] >= min_n and m["roi"] > best_roi:
            best_tau, best_roi = tau, m["roi"]
    return best_tau


# ---- Part 1 診断 -----------------------------------------------------------


def run_diagnostic(races: list[Race], beta: float) -> dict:
    """IS全体: 予測エッジ別の実ROI＋τ=0で全+edge馬を賭けたときの成績。"""
    logger.info(
        f"=== Part 1 診断: 確率校正＋エッジ別 実ROI（IS {IS_START}-{IS_END}） ==="
    )
    logger.info(f"  校正温度 β = {beta:.4f}")
    agg = aggregate_by_edge(races, IS_START, IS_END, beta)
    logger.info(f"  {'予測edge帯':<12}{'N':>9}{'的中率':>9}{'実ROI':>10}")
    for label in EDGE_LABELS:
        m = agg[label]
        logger.info(
            f"  {label:<12}{m['n']:>9,}{m['hit'] * 100:>8.1f}%{m['roi'] * 100:>9.1f}%"
        )
    tau0 = collect_edge_bets(races, IS_START, IS_END, beta, 0.0)
    logger.info(
        f"  → τ=0（全+edge馬を強制ベット）: N={tau0['n']:,} / "
        f"的中率 {tau0['hit'] * 100:.1f}% / ROI {tau0['roi'] * 100:.1f}%"
    )
    return {"beta": beta, "by_edge": agg, "tau0_all_plus_edge": tau0}


# ---- Part 2 IS内CV ---------------------------------------------------------


def run_cv(races: list[Race], min_n: int, mode: str) -> dict:
    """学習区間で β・τ を決め、未見の検証区間で採点する（過学習補正後の実力）。"""
    mode_label = "best=1レース最良1頭" if mode == "best" else "all=全+edge馬"
    logger.info(
        f"=== Part 2 IS内CV [{mode_label}]: 学習で β・τ 決定（N≥{min_n}）→ 検証で採点"
        f"（{len(CV_FOLDS)}フォールド） ==="
    )
    folds = []
    for tr_s, tr_e, va_s, va_e in CV_FOLDS:
        beta = fit_beta(races, tr_s, tr_e)
        tau = select_tau(races, tr_s, tr_e, beta, min_n, mode)
        train_sel = collect_edge_bets(races, tr_s, tr_e, beta, tau, mode)
        valid_sel = collect_edge_bets(races, va_s, va_e, beta, tau, mode)
        # 検証区間で賭け可能な全馬数（被覆率の分母）。
        valid_all = collect_edge_bets(races, va_s, va_e, beta, -float("inf"), mode)
        coverage = valid_sel["n"] / valid_all["n"] if valid_all["n"] else 0.0
        gap = train_sel["roi"] - valid_sel["roi"]
        folds.append(
            {
                "train": [tr_s, tr_e],
                "valid": [va_s, va_e],
                "beta": beta,
                "tau": tau,
                "train_sel": train_sel,
                "valid_sel": valid_sel,
                "coverage": coverage,
                "gap": gap,
            }
        )
        logger.info(
            f"  学習{tr_s}-{tr_e}→検証{va_s}-{va_e}: β={beta:.4f} / τ={tau:.2f}"
        )
        logger.info(
            f"    学習(選択)ROI {train_sel['roi'] * 100:.1f}%(N={train_sel['n']}) / "
            f"検証(選択)ROI {valid_sel['roi'] * 100:.1f}%(N={valid_sel['n']}) "
            f"(過学習ギャップ {gap * 100:+.1f}pt) / "
            f"被覆 {valid_sel['n']}/{valid_all['n']} ({coverage * 100:.0f}%)"
        )

    # 検証(選択)ROI を被覆ベット数で加重平均＝過学習補正後の実力見積もり。
    total_cov_n = sum(f["valid_sel"]["n"] for f in folds)
    valid_sel_overall = (
        sum(f["valid_sel"]["roi"] * f["valid_sel"]["n"] for f in folds) / total_cov_n
        if total_cov_n
        else 0.0
    )
    logger.info(
        f"  → 検証(選択)ROI加重平均（実力見積もり）: {valid_sel_overall * 100:.1f}% "
        f"/ 総被覆ベット数: {total_cov_n}"
    )
    return {"mode": mode, "folds": folds, "valid_sel_roi_overall": valid_sel_overall}


def load_base_weights() -> tuple[dict, float, str]:
    """スコアの固定重みを決める: Stage B ロバスト重みを優先、無ければ DEFAULT。"""
    if ROBUST_PATH.exists():
        data = json.loads(ROBUST_PATH.read_text(encoding="utf-8"))
        best = data.get("best", {})
        if "weights" in best and "wr_blend" in best:
            return best["weights"], best["wr_blend"], "robust_weights.json"
    return DEFAULT_WEIGHTS, DEFAULT_WR_BLEND, "DEFAULT"


def main() -> None:
    """確率校正＋エッジベットの診断とIS内CVを実行し、レポートを保存する（OOS封印）。"""
    ap = argparse.ArgumentParser(description="#B5 Stage C 確率校正＋エッジベット検証")
    ap.add_argument(
        "--min-n", type=int, default=30, help="τ採用に必要な学習区間ベット数の下限"
    )
    args = ap.parse_args()

    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    try:
        cache = load_races(conn)
    finally:
        conn.close()

    weights, wr_blend, src = load_base_weights()
    logger.info(f"スコア固定重み: {src}（wr_blend={wr_blend:.3f}）")
    races = build_races(cache, weights, wr_blend)

    beta_is = fit_beta(races, IS_START, IS_END)
    diagnostic = run_diagnostic(races, beta_is)
    cv_all = run_cv(races, args.min_n, "all")
    cv_best = run_cv(races, args.min_n, "best")

    out = {
        "stage": "B5-C",
        "is_period": [IS_START, IS_END],
        "weights_source": src,
        "wr_blend": wr_blend,
        "tau_grid": TAU_GRID,
        "min_n": args.min_n,
        "beta_is": beta_is,
        "diagnostic": diagnostic,
        "cross_validation_all": cv_all,
        "cross_validation_best": cv_best,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

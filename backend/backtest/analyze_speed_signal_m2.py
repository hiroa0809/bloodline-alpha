"""Direction A M2: 本格スピード指数（クラス補正＋馬場差）＋馬場適性 信号診断。

M1（analyze_speed_signal.py）は素朴スピード指標＝走破タイムを (競馬場×馬場種別×距離) の
as-of 標準で基準化しただけで、③オッズ整合AUC（市場を揃えた増分エッジ）が直近ブロック
(2007-2013)で 0.517-0.520 へ低下しゲート不通過だった。本 M2 は M1 の留保「素朴版ゆえ
edge皆無ではない」を検証するため、以下を加えた本格スピード指数で③を再判定する。

  1. クラス補正  : 標準タイムのキーに競走条件（未勝利/1勝/2勝/3勝/OP）を追加。
  2. 馬場差(日次): その日・競馬場・芝/ダート別の実測中央オフセットを差し引く（速度指数の
                   正統な track-variant。フィット係数なし＝中央値）。
  3. 馬場適性    : 上記補正でタイムから馬場の影響を「消す」のとは別に、馬ごとの重・不良
                   適性（得意/不得意の両方向）を符号付きで捉える独立特徴。市場が未織り込みの
                   材料の有力候補。OFF馬場レースに限定して③を測る。過去OFF走数しきい値
                   （≥1/≥2/≥3）の感度変種を併載し、サンプル不足由来のブレかを切り分ける。

仮説と検証は M1 と同一: ①生AUC（予測力）と ③オッズ整合AUC（市場制御後の増分）を
analyze_subscore_signal の AUC 機構でそのまま評価する。特徴量構築は speed_m2_core に分離。

金庫ルール厳守:
  - 対象＝非新馬の平地戦・IS(1993-2013)限定・OOS(2014+)封印（SQLでも境界封印）。
  - point-in-time: 馬の過去走は対象レース日より厳密に前、標準は対象年より前の年のみ。
    日次トラック差は「過去走の当日（完了済み）」で作るため未来リークなし（その過去走は
    予測対象の将来レースより前に終わっている）。
  - しきい値（top_n / odds_ratio / auc_min / MIN_DAY_RUNS / 馬場バケツ / 過去OFF走数）は
    事前登録。既存ライブスコア・M1 には触れない。

使い方:
    python backend/backtest/analyze_speed_signal_m2.py
    python backend/backtest/analyze_speed_signal_m2.py --smoke   # 小窓・実行可否のみ
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
from backtest.analyze_subscore_signal import (  # noqa: E402
    IS_END,
    IS_START,
    _consistency_blocks,
    auc_only,
    auc_over_races,
    market_builder,
    quintile_lift,
    race_indices,
)
from backtest.speed_m2_core import (  # noqa: E402
    GOING_FLOORS,
    MIN_DAY_RUNS,
    FEATURES,
    assign_figures,
    assign_horse_features,
    build_arrays,
    load_race_info,
    load_runs,
    race_indices_off,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "speed_signal_m2_report.json"

SCAN_START = 1986
SMOKE_SCAN_START, SMOKE_ANA_START, SMOKE_ANA_END = 2005, 2008, 2010

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ============================================================
# 解析
# ============================================================


def analyze(a, ana_start, ana_end, top_n, odds_ratio, auc_min, min_day_runs) -> dict:
    """全変種を①②③④で評価し go/no-go 判定付きレポートを返す。"""
    idx_all = {
        "pooled": race_indices(a, ana_start, ana_end),
        "blocks": [
            (b, race_indices(a, b[0], b[1]))
            for b in _consistency_blocks(ana_start, ana_end)
        ],
    }
    idx_off = {
        "pooled": race_indices_off(a, ana_start, ana_end),
        "blocks": [
            (b, race_indices_off(a, b[0], b[1]))
            for b in _consistency_blocks(ana_start, ana_end)
        ],
    }

    mvals, mmask = market_builder(a)
    # 市場参照AUC は診断スコープ別に出す（OFF限定特徴は OFF馬場の市場と比較する）。
    market_by_scope = {
        "all": auc_over_races(a, mvals, mmask, idx_all["pooled"], top_n, odds_ratio)[
            "auc"
        ],
        "off": auc_over_races(a, mvals, mmask, idx_off["pooled"], top_n, odds_ratio)[
            "auc"
        ],
    }
    market_pooled = market_by_scope["all"]
    logger.info(f"市場参照AUC（1/オッズ・TOP{top_n}・全馬場）: {market_pooled:.4f}")

    # スピード変種（all）＋ 馬場適性の過去OFF走数しきい値変種（off）を一括診断する。
    feat_specs = [(name, label, scope) for name, _, label, scope in FEATURES]
    feat_specs += [
        (f"going_apt_off{fl}", f"馬場適性≥{fl}OFF走", "off") for fl in GOING_FLOORS
    ]

    results = []
    for fname, label, scope in feat_specs:
        sel = idx_off if scope == "off" else idx_all
        vals, mask = a[fname], a[f"{fname}_m"]
        pooled = auc_over_races(a, vals, mask, sel["pooled"], top_n, odds_ratio)
        bstats = [
            {"block": list(b), **auc_over_races(a, vals, mask, ix, top_n, odds_ratio)}
            for b, ix in sel["blocks"]
        ]
        block_aucs = [x["auc"] for x in bstats]
        block_maucs = [x["matched_auc"] for x in bstats]
        consistent = bool(block_aucs) and all(
            (not np.isnan(x)) and x > 0.5 for x in block_aucs
        )
        matched_consistent = bool(block_maucs) and all(
            (not np.isnan(x)) and x > auc_min for x in block_maucs
        )
        if np.isnan(pooled["auc"]) or pooled["auc"] < 0.5 or not consistent:
            verdict = "予測力なし"
        elif matched_consistent:
            verdict = "エッジ候補"
        else:
            verdict = "市場超えなし"
        # 被覆（診断スコープ内でデータ有り＆有効着の頭数）
        coverage = 0
        for h0, h1 in sel["pooled"]:
            coverage += int((mask[h0:h1] & (a["chaku"][h0:h1] >= 1)).sum())
        results.append(
            {
                "feature": fname,
                "name": label,
                "scope": scope,
                "verdict": verdict,
                "pooled_auc": pooled["auc"],
                "market_auc": market_by_scope[scope],
                "matched_auc": pooled["matched_auc"],
                "matched_pairs": pooled["matched_pairs"],
                "coverage": coverage,
                "consistent": consistent,
                "matched_consistent": matched_consistent,
                "auc_top1": auc_only(a, vals, mask, sel["pooled"], 1),
                "block_aucs": block_aucs,
                "block_matched_aucs": block_maucs,
                "quintile_lift": quintile_lift(a, vals, mask, sel["pooled"], top_n),
            }
        )

    _log_table(results, top_n)
    # 馬場適性の過去OFF走数 分布（サンプル不足の切り分け用・OFF馬場×適性定義済み×有効着）。
    offc = a.get("_going_offc")
    base_m = a.get("going_apt_off1_m")
    offc_dist = {"1": 0, "2": 0, "3+": 0}
    if offc is not None and base_m is not None:
        for h0, h1 in idx_off["pooled"]:
            sl = base_m[h0:h1] & (a["chaku"][h0:h1] >= 1)
            v = offc[h0:h1][sl]
            offc_dist["1"] += int((v == 1).sum())
            offc_dist["2"] += int((v == 2).sum())
            offc_dist["3+"] += int((v >= 3).sum())
    logger.info(f"馬場適性 過去OFF走数 分布（被覆頭）: {offc_dist}")

    speed_edge = [
        r
        for r in results
        if r["feature"] in ("soha_best", "soha_recent", "soha_avg")
        and r["verdict"] == "エッジ候補"
    ]
    apt_results = [r for r in results if r["feature"].startswith("going_apt_off")]
    apt_edge = [r for r in apt_results if r["verdict"] == "エッジ候補"]
    gate_pass = len(speed_edge) > 0 or len(apt_edge) > 0
    apt_summary = " / ".join(
        f"{r['name']}:被覆{r['coverage']:,}・③"
        f"{('—' if np.isnan(r['matched_auc']) else format(r['matched_auc'], '.3f'))}"
        for r in apt_results
    )
    logger.info(
        f"=== go/no-go: 補正スピード{'通過' if speed_edge else '不通過'} / "
        f"馬場適性{'通過' if apt_edge else '不通過'} ==="
    )
    logger.info(f"  馬場適性 感度: {apt_summary}")
    if not gate_pass:
        logger.info(
            "  → クラス・馬場差・馬場適性でも市場超え増分なし（≈互角）。スピード路線は畳む候補"
        )
    return {
        "analysis_period": [ana_start, ana_end],
        "is_blocks": [list(b) for b in _consistency_blocks(ana_start, ana_end)],
        "top_n": top_n,
        "odds_ratio": odds_ratio,
        "auc_min": auc_min,
        "min_day_runs": min_day_runs,
        "going_floors": list(GOING_FLOORS),
        "market_reference_auc": market_pooled,
        "going_apt_offc_dist": offc_dist,
        "features": results,
        "gate_pass_speed": len(speed_edge) > 0,
        "gate_pass_aptitude": len(apt_edge) > 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _log_table(results: list[dict], top_n: int) -> None:
    """①AUC降順サマリーをログ出力。"""
    logger.info(f"=== M2 スピード信号診断（TOP{top_n}入賞・①AUC） ===")
    logger.info(
        f"  {'特徴':<16}{'範囲':>5}{'①AUC':>8}{'市場②':>8}{'③整合':>8}{'被覆':>9}{'一貫':>5}  判定"
    )
    for r in sorted(
        results,
        key=lambda x: x["pooled_auc"] if not np.isnan(x["pooled_auc"]) else -1,
        reverse=True,
    ):
        mauc = "—" if np.isnan(r["matched_auc"]) else f"{r['matched_auc']:.3f}"
        auc = "—" if np.isnan(r["pooled_auc"]) else f"{r['pooled_auc']:.3f}"
        logger.info(
            f"  {r['name']:<16}{r['scope']:>5}{auc:>8}{r['market_auc']:>8.3f}"
            f"{mauc:>8}{r['coverage']:>9,}{'○' if r['consistent'] else '×':>5}  {r['verdict']}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Direction A M2: 本格スピード指数＋馬場適性 信号診断（IS限定・OOS封印）"
    )
    ap.add_argument("--top-n", type=int, default=3, help="入賞ラベル順位上限（既定3）")
    ap.add_argument(
        "--odds-ratio", type=float, default=1.5, help="③オッズ整合の許容比（既定1.5）"
    )
    ap.add_argument(
        "--auc-min", type=float, default=0.52, help="エッジ判定のAUC下限（既定0.52）"
    )
    ap.add_argument(
        "--min-day-runs",
        type=int,
        default=MIN_DAY_RUNS,
        help=f"日次トラック差を適用する当日完走数の下限（既定{MIN_DAY_RUNS}）",
    )
    ap.add_argument(
        "--smoke", action="store_true", help="小窓で実行可否のみ確認（本番扱いしない）"
    )
    args = ap.parse_args()
    min_day_runs = args.min_day_runs

    if args.smoke:
        scan_start, ana_start, ana_end = (
            SMOKE_SCAN_START,
            SMOKE_ANA_START,
            SMOKE_ANA_END,
        )
    else:
        scan_start, ana_start, ana_end = SCAN_START, IS_START, IS_END
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

    assign_figures(runs, min_day_runs)
    assign_horse_features(runs)
    a = build_arrays(runs, ana_end)

    report = analyze(
        a, ana_start, ana_end, args.top_n, args.odds_ratio, args.auc_min, min_day_runs
    )
    if args.smoke:
        # スモークではクラス tier 分布の妥当性も確認（結果の良し悪しは判断材料にしない）
        from collections import Counter

        tiers = Counter(r["class_tier"] for r in runs if not r["is_maiden"])
        logger.info(
            f"スモーク: クラスtier分布(非新馬)="
            f"{dict(sorted(tiers.items(), key=lambda kv: (kv[0] is None, kv[0])))}"
        )
        logger.info("スモーク完了（結果の良し悪しは判断材料にしない）。")
        return
    OUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

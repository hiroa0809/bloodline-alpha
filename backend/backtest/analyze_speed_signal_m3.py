"""Direction A M3a: 馬ごとペース／トリップ補正スピード 信号診断。

M2（analyze_speed_signal_m2）でクラス補正＋馬場差の補正後指数 cv_soha が走破直近の③
オッズ整合AUC＝直近ブロック0.524（ゲート通過・薄い増分）まで来た。M3a はこれを鋭くする
精緻化として、馬ごとの「トリップ（展開の不利）」を独立特徴で③診断する。設計の核・特徴
定義・point-in-time は speed_m3_core の docstring 参照。

M2 ハーネスはコピーせず、同じ AUC 機構（analyze_subscore_signal）と M2 パイプライン
（speed_m2_core）を import して薄く orchestrate する。サニティとして M2 の補正
soha_recent の③を同じ装置で再算出し、M2 確定値（直近ブロック 0.524）と一致するか併記する。

ゲート: trip 変種の③が3ブロック全部で >auc_min なら verdict「エッジ候補」＝M3b（一般戦
エッジベット運用）へ。不通過なら、クラス・馬場差・トリップでも市場超え増分は薄い（≈互角）。

金庫ルール厳守:
  - 対象＝非新馬の平地戦・IS(1993-2013)限定・OOS(2014+)封印（ana_end<=IS_END ガード）。
  - しきい値（BACK_CUT / TRIP_FLOORS / top_n / odds_ratio / auc_min）は事前登録。
    既存ライブスコア・M1・M2 には触れない。

使い方:
    python backend/backtest/analyze_speed_signal_m3.py
    python backend/backtest/analyze_speed_signal_m3.py --smoke   # 小窓・実行可否のみ
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
    MIN_DAY_RUNS,
    assign_figures,
    assign_horse_features,
    build_arrays,
    load_race_info,
    load_runs,
)
from backtest.speed_m3_core import (  # noqa: E402
    BACK_CUT,
    TRIP_FLOORS,
    assign_trip_features,
    build_trip_arrays,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "speed_signal_m3_report.json"

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


def _verdict(pooled_auc, block_aucs, block_maucs, auc_min) -> tuple[str, bool, bool]:
    """M2 と同じ判定: 生①一貫＆③(matched)一貫で「エッジ候補」。"""
    consistent = bool(block_aucs) and all(
        (not np.isnan(x)) and x > 0.5 for x in block_aucs
    )
    matched_consistent = bool(block_maucs) and all(
        (not np.isnan(x)) and x > auc_min for x in block_maucs
    )
    if np.isnan(pooled_auc) or pooled_auc < 0.5 or not consistent:
        return "予測力なし", consistent, matched_consistent
    if matched_consistent:
        return "エッジ候補", consistent, matched_consistent
    return "市場超えなし", consistent, matched_consistent


def _diagnose(a, vals, mask, idx, top_n, odds_ratio, auc_min) -> dict:
    """1特徴を①②③④＋3ブロック一貫ゲートで評価（M2 analyze と同型）。"""
    pooled = auc_over_races(a, vals, mask, idx["pooled"], top_n, odds_ratio)
    bstats = [
        {"block": list(b), **auc_over_races(a, vals, mask, ix, top_n, odds_ratio)}
        for b, ix in idx["blocks"]
    ]
    block_aucs = [x["auc"] for x in bstats]
    block_maucs = [x["matched_auc"] for x in bstats]
    verdict, consistent, matched_consistent = _verdict(
        pooled["auc"], block_aucs, block_maucs, auc_min
    )
    coverage = 0
    for h0, h1 in idx["pooled"]:
        coverage += int((mask[h0:h1] & (a["chaku"][h0:h1] >= 1)).sum())
    return {
        "verdict": verdict,
        "pooled_auc": pooled["auc"],
        "matched_auc": pooled["matched_auc"],
        "matched_pairs": pooled["matched_pairs"],
        "coverage": coverage,
        "consistent": consistent,
        "matched_consistent": matched_consistent,
        "auc_top1": auc_only(a, vals, mask, idx["pooled"], 1),
        "block_aucs": block_aucs,
        "block_matched_aucs": block_maucs,
        "quintile_lift": quintile_lift(a, vals, mask, idx["pooled"], top_n),
    }


def analyze(a, trip_specs, ana_start, ana_end, top_n, odds_ratio, auc_min) -> dict:
    """トリップ特徴群を③主軸で診断し go/no-go 判定付きレポートを返す。"""
    idx = {
        "pooled": race_indices(a, ana_start, ana_end),
        "blocks": [
            (b, race_indices(a, b[0], b[1]))
            for b in _consistency_blocks(ana_start, ana_end)
        ],
    }
    mvals, mmask = market_builder(a)
    market_auc = auc_over_races(a, mvals, mmask, idx["pooled"], top_n, odds_ratio)[
        "auc"
    ]
    logger.info(f"市場参照AUC（1/オッズ・TOP{top_n}・全馬場）: {market_auc:.4f}")

    # --- サニティ: M2 補正 soha_recent の③を同じ装置で再算出（M2確定値と一致するはず） ---
    sanity = None
    if "soha_recent" in a:
        s = _diagnose(
            a, a["soha_recent"], a["soha_recent_m"], idx, top_n, odds_ratio, auc_min
        )
        sanity = {
            "feature": "soha_recent(M2補正)",
            "pooled_auc": s["pooled_auc"],
            "matched_auc": s["matched_auc"],
            "block_matched_aucs": s["block_matched_aucs"],
        }
        msg = (
            f"サニティ M2 soha_recent: ③pooled={s['matched_auc']:.4f} / "
            f"③blocks={[round(x, 3) for x in s['block_matched_aucs']]}"
        )
        # M2確定値 [0.527,0.530,0.524] は full-IS・既定設定の数値。--smoke や引数上書き時は
        # 窓もブロックも違い一致しないため、その条件のときだけ照合基準を併記する。
        if (
            ana_start == IS_START
            and ana_end == IS_END
            and top_n == 3
            and odds_ratio == 1.5
        ):
            msg += "（M2確定 [0.527, 0.530, 0.524] と照合）"
        logger.info(msg)

    results = []
    for fname, label in trip_specs:
        d = _diagnose(a, a[fname], a[f"{fname}_m"], idx, top_n, odds_ratio, auc_min)
        d.update({"feature": fname, "name": label, "market_auc": market_auc})
        results.append(d)

    _log_table(results, top_n)
    _log_distributions(a, idx)

    edge = [r for r in results if r["verdict"] == "エッジ候補"]
    gate_pass = len(edge) > 0
    logger.info(
        f"=== go/no-go: トリップ補正スピード {'通過' if gate_pass else '不通過'} ==="
    )
    if not gate_pass:
        logger.info(
            "  → トリップ補正でも市場超え増分は閾値未達（≈互角）。M2 から厚くならず"
        )
    else:
        logger.info(f"  → エッジ候補: {', '.join(r['name'] for r in edge)} → M3b 検討")
    return {
        "analysis_period": [ana_start, ana_end],
        "is_blocks": [list(b) for b in _consistency_blocks(ana_start, ana_end)],
        "top_n": top_n,
        "odds_ratio": odds_ratio,
        "auc_min": auc_min,
        "back_cut": BACK_CUT,
        "trip_floors": list(TRIP_FLOORS),
        "market_reference_auc": market_auc,
        "sanity_m2_soha_recent": sanity,
        "features": results,
        "gate_pass": gate_pass,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _log_table(results: list[dict], top_n: int) -> None:
    """①AUC降順サマリーをログ出力（M2 と同レイアウト）。"""
    logger.info(f"=== M3a トリップ信号診断（TOP{top_n}入賞・①AUC） ===")
    logger.info(
        f"  {'特徴':<20}{'①AUC':>8}{'市場②':>8}{'③整合':>8}{'被覆':>9}{'一貫':>5}  判定"
    )
    for r in sorted(
        results,
        key=lambda x: x["pooled_auc"] if not np.isnan(x["pooled_auc"]) else -1,
        reverse=True,
    ):
        mauc = "—" if np.isnan(r["matched_auc"]) else f"{r['matched_auc']:.3f}"
        auc = "—" if np.isnan(r["pooled_auc"]) else f"{r['pooled_auc']:.3f}"
        logger.info(
            f"  {r['name']:<20}{auc:>8}{r['market_auc']:>8.3f}"
            f"{mauc:>8}{r['coverage']:>9,}{'○' if r['consistent'] else '×':>5}  {r['verdict']}"
        )


def _log_distributions(a: dict, idx: dict) -> None:
    """後方走数分布・mae3f 充足率（サンプル不足の切り分け）。"""
    tc = a.get("_trip_c")
    have = a.get("_mae3f_have")
    if tc is None:
        return
    d = {">=1": 0, ">=2": 0, ">=3": 0}
    mae_have = mae_tot = 0
    for h0, h1 in idx["pooled"]:
        valid = a["chaku"][h0:h1] >= 1
        v = tc[h0:h1][valid]
        d[">=1"] += int((v >= 1).sum())
        d[">=2"] += int((v >= 2).sum())
        d[">=3"] += int((v >= 3).sum())
        if have is not None:
            mae_have += int(have[h0:h1][valid].sum())
            mae_tot += int(valid.sum())
    logger.info(f"後方トリップ過去走数 分布（有効着頭）: {d}")
    if mae_tot:
        logger.info(
            f"mae3f 充足率（有効着頭・ペース副変種の母集団）: "
            f"{mae_have:,}/{mae_tot:,} ({100 * mae_have / mae_tot:.1f}%)"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Direction A M3a: トリップ補正スピード 信号診断（IS限定・OOS封印）"
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

    assign_figures(runs, args.min_day_runs)
    assign_horse_features(runs)  # cv_soha 履歴（M2）
    assign_trip_features(runs)  # 後方トリップ履歴（M3）
    a = build_arrays(runs, ana_end)
    trip_specs = build_trip_arrays(a, runs, ana_end)

    report = analyze(
        a, trip_specs, ana_start, ana_end, args.top_n, args.odds_ratio, args.auc_min
    )
    if args.smoke:
        logger.info("スモーク完了（結果の良し悪しは判断材料にしない）。")
        return
    OUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

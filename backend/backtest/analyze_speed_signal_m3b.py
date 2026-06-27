"""Direction A M3b: 上がり（末脚）速度＋大まくり（位置上昇）信号診断。

ユーザー要件「出遅れても最後の直線で大まくりする馬のスピードが反映されるか」を検証。設計の核・
特徴定義・point-in-time は speed_m3b_core の docstring 参照。速度の軸を走破タイム(soha＝出遅れで
減点)から上がり(ato_3f＝スタート無関係の末脚)へ移し、「最後の位置上昇＝大まくり」を独立特徴にする。

M2/M3a ハーネスはコピーせず、同じ AUC 機構（analyze_subscore_signal）と M2 パイプライン
（speed_m2_core）＋ M3a の `_diagnose` を import して薄く orchestrate する。

2系統で診断する:
  - 主（full-IS 1993-2013）: close_*（末脚速度）/ makuri_*（大まくり）を①②③④＋3ブロック一貫ゲート。
  - 補助（2000-2013・事前登録 SP_WINDOW）: slowpace_*（緩ペース上がり）。mae_3f は2000年より前が
    完全に空（block1=0%）でfull-ISゲートに載らないため、データのある2000-2013に限定した別ゲートで診断。

サニティ: 市場参照AUC（≈0.799期待）＋ M2 ato3f_best の③を同装置で再算出しM2確定値[0.525,0.529,0.522]
と照合（full-IS既定設定のときだけ併記）。ゲート: 主の close_*/makuri_* に verdict「エッジ候補」があれば通過。

金庫: 非新馬平地・IS1993-2013限定・OOS封印（ana_end<=IS_END ガード）。補助SP_WINDOWもIS内。
しきい値（BACK_CUT/GAIN_MIN/severity符号/TRIP_FLOORS/SP_WINDOW_START/top_n/odds_ratio/auc_min）は事前登録。

使い方:
    python backend/backtest/analyze_speed_signal_m3b.py
    python backend/backtest/analyze_speed_signal_m3b.py --smoke   # 小窓・実行可否のみ
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
from backtest.analyze_speed_signal_m3 import _diagnose  # noqa: E402
from backtest.analyze_subscore_signal import (  # noqa: E402
    IS_END,
    IS_START,
    _consistency_blocks,
    auc_over_races,
    market_builder,
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
from backtest.speed_m3b_core import (  # noqa: E402
    BACK_CUT,
    GAIN_MIN,
    assign_close_features,
    build_close_arrays,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "speed_signal_m3b_report.json"

SCAN_START = 1986
SP_WINDOW_START = (
    2000  # mae_3f は2000年より前が空のため補助ゲートはここから（事前登録）
)
SMOKE_SCAN_START, SMOKE_ANA_START, SMOKE_ANA_END = 2005, 2008, 2010

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _build_idx(a, y0, y1) -> dict:
    """pooled＋3ブロックの頭スライス（M2/M3a の analyze と同型）。"""
    return {
        "pooled": race_indices(a, y0, y1),
        "blocks": [
            (b, race_indices(a, b[0], b[1])) for b in _consistency_blocks(y0, y1)
        ],
    }


def _diag_group(a, specs, idx, market_auc, top_n, odds_ratio, auc_min) -> list[dict]:
    """spec 群を _diagnose で評価し market_auc を添える。"""
    out = []
    for fname, label in specs:
        d = _diagnose(a, a[fname], a[f"{fname}_m"], idx, top_n, odds_ratio, auc_min)
        d.update({"feature": fname, "name": label, "market_auc": market_auc})
        out.append(d)
    return out


def analyze(a, spec_groups, ana_start, ana_end, top_n, odds_ratio, auc_min) -> dict:
    """末脚／大まくり（主・full-IS）と緩ペース上がり（補助・2000-13）を診断。"""
    idx_full = _build_idx(a, ana_start, ana_end)
    mvals, mmask = market_builder(a)
    market_auc = auc_over_races(a, mvals, mmask, idx_full["pooled"], top_n, odds_ratio)[
        "auc"
    ]
    logger.info(f"市場参照AUC（1/オッズ・TOP{top_n}・全馬場）: {market_auc:.4f}")

    # サニティ: M2 ato3f_best の③を同装置で再算出（M2確定値と照合）。
    sanity = None
    if "ato3f_best" in a:
        s = _diagnose(
            a, a["ato3f_best"], a["ato3f_best_m"], idx_full, top_n, odds_ratio, auc_min
        )
        sanity = {
            "feature": "ato3f_best(M2補正)",
            "matched_auc": s["matched_auc"],
            "block_matched_aucs": s["block_matched_aucs"],
        }
        msg = (
            f"サニティ M2 ato3f_best: ③pooled={s['matched_auc']:.4f} / "
            f"③blocks={[round(x, 3) for x in s['block_matched_aucs']]}"
        )
        if (
            ana_start == IS_START
            and ana_end == IS_END
            and top_n == 3
            and odds_ratio == 1.5
        ):
            msg += "（M2確定 [0.525, 0.529, 0.522] と照合）"
        logger.info(msg)

    # 主: full-IS で末脚・大まくり。
    full = _diag_group(
        a, spec_groups["full"], idx_full, market_auc, top_n, odds_ratio, auc_min
    )
    _log_table("M3b 主診断（末脚＋大まくり・full-IS）", full, top_n)

    # 補助: 2000-2013（mae_3f データ制約）で緩ペース上がり。
    sp_start = max(SP_WINDOW_START, ana_start)
    idx_sp = _build_idx(a, sp_start, ana_end)
    sp = _diag_group(
        a, spec_groups["sp"], idx_sp, market_auc, top_n, odds_ratio, auc_min
    )
    logger.info(
        f"--- 補助診断は mae_3f データ制約により {sp_start}-{ana_end} 限定（block1=0%のため）---"
    )
    _log_table(f"M3b 補助診断（緩ペース上がり・{sp_start}-{ana_end}）", sp, top_n)

    _log_distributions(a, idx_full)

    edge = [r for r in full if r["verdict"] == "エッジ候補"]
    gate_pass = len(edge) > 0
    logger.info(
        f"=== go/no-go（主・末脚＋大まくり）: {'通過' if gate_pass else '不通過'} ==="
    )
    if gate_pass:
        logger.info(f"  → エッジ候補: {', '.join(r['name'] for r in edge)} → 次段検討")
    else:
        logger.info(
            "  → 末脚・大まくりでも市場超え増分は閾値未達（≈互角）。出遅れ馬の速度は"
            "数値化できても市場が織り込み済みの可能性"
        )
    return {
        "analysis_period": [ana_start, ana_end],
        "is_blocks": [list(b) for b in _consistency_blocks(ana_start, ana_end)],
        "sp_window": [sp_start, ana_end],
        "sp_blocks": [list(b) for b in _consistency_blocks(sp_start, ana_end)],
        "top_n": top_n,
        "odds_ratio": odds_ratio,
        "auc_min": auc_min,
        "back_cut": BACK_CUT,
        "gain_min": GAIN_MIN,
        "market_reference_auc": market_auc,
        "sanity_m2_ato3f_best": sanity,
        "features_full": full,
        "features_sp": sp,
        "gate_pass": gate_pass,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _log_table(title: str, results: list[dict], top_n: int) -> None:
    """①AUC降順サマリー（M2/M3a と同レイアウト）。"""
    logger.info(f"=== {title}（TOP{top_n}入賞・①AUC） ===")
    logger.info(
        f"  {'特徴':<22}{'①AUC':>8}{'市場②':>8}{'③整合':>8}{'被覆':>9}{'一貫':>5}  判定"
    )
    for r in sorted(
        results,
        key=lambda x: x["pooled_auc"] if not np.isnan(x["pooled_auc"]) else -1,
        reverse=True,
    ):
        mauc = "—" if np.isnan(r["matched_auc"]) else f"{r['matched_auc']:.3f}"
        auc = "—" if np.isnan(r["pooled_auc"]) else f"{r['pooled_auc']:.3f}"
        logger.info(
            f"  {r['name']:<22}{auc:>8}{r['market_auc']:>8.3f}"
            f"{mauc:>8}{r['coverage']:>9,}{'○' if r['consistent'] else '×':>5}  {r['verdict']}"
        )


def _log_distributions(a: dict, idx: dict) -> None:
    """大まくり走数分布・mae3f 充足率（サンプル不足/データ制約の切り分け）。"""
    mc = a.get("_makuri_c")
    have = a.get("_mae3f_have")
    if mc is None:
        return
    d = {">=1": 0, ">=2": 0, ">=3": 0}
    mae_have = mae_tot = 0
    for h0, h1 in idx["pooled"]:
        valid = a["chaku"][h0:h1] >= 1
        v = mc[h0:h1][valid]
        d[">=1"] += int((v >= 1).sum())
        d[">=2"] += int((v >= 2).sum())
        d[">=3"] += int((v >= 3).sum())
        if have is not None:
            mae_have += int(have[h0:h1][valid].sum())
            mae_tot += int(valid.sum())
    logger.info(f"大まくり過去走数 分布（有効着頭）: {d}")
    if mae_tot:
        logger.info(
            f"mae3f 充足率（有効着頭・緩ペース補助の母集団）: "
            f"{mae_have:,}/{mae_tot:,} ({100 * mae_have / mae_tot:.1f}%)"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Direction A M3b: 末脚＋大まくり 信号診断（IS限定・OOS封印）"
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
    assign_horse_features(runs)  # M2 features（build_arrays / サニティ ato3f_best 用）
    assign_close_features(runs)  # M3b 末脚・大まくり履歴
    a = build_arrays(runs, ana_end)
    spec_groups = build_close_arrays(a, runs, ana_end)

    report = analyze(
        a, spec_groups, ana_start, ana_end, args.top_n, args.odds_ratio, args.auc_min
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

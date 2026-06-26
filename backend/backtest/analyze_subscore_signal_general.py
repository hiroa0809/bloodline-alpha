"""Direction A トラックA: 一般戦のサブ項目＋スピード＋総合スコア 信号診断（③主軸）。

新馬戦版（analyze_subscore_signal.py）を一般戦（非新馬・平地戦）へ土俵移しした版。
backtest_subscore_cache_general（precompute_general.py が生成）を読み、以下を1個ずつ
①レース内AUC / ②市場参照 / ③オッズ整合AUC（市場を揃えた後の増分＝核心）/ ④五分位
で評価する:

  - ファンダ14次元（A1-A5/B1-B4/C1-C3/E1-E2）… 新馬戦で除外したA4/A5/E1も一般戦で再判定。
  - スピード4変種（走破 best/recent/avg・上り3F best）… 生値（AUCは順序のみ使う）。
  - 総合スコア2変種（ファンダのみ / ファンダ＋スピード）… 固定配点で束ねた1次元として③評価。
    束ねても市場超え増分が出るかを直接見る＝「①生の予測力で良く見えるだけ」を回避。

トラックB（optimize_top1/run_backtest の配点ISテスト）と並走し、③（本診断）が伴わずに
配点ROI/Top-1だけ良く見えるなら過去の轍（市場織り込み）の再現、という比較に使う。

金庫ルール厳守:
  - IS（1993-2013）限定。OOS は load 時に SQL（year_max）で読み込まない（封印）。
  - しきい値（top_n / odds_ratio / auc_min＝3 / 1.5 / 0.52）は事前登録・据え置き。
  - 計測装置のため CodeRabbit 通過後に本番確定（CLAUDE.md「設計・計測タスクの順序」）。

使い方:
    python backend/backtest/analyze_subscore_signal_general.py
    python backend/backtest/analyze_subscore_signal_general.py --start-year 2012 --end-year 2013  # スモーク
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
from backtest import top1_core as core  # noqa: E402
from backtest.analyze_subscore_signal import (  # noqa: E402
    IS_END,
    IS_START,
    _NAMES,
    analyze,
    build_dimensions,
)
from backtest.run_backtest import DEFAULT_WEIGHTS, DEFAULT_WR_BLEND  # noqa: E402

CACHE_GENERAL = "backtest_subscore_cache_general"
OUT_PATH = _BACKEND_DIR / "backtest" / "subscore_signal_general_report.json"

# 総合スコア（ファンダ＋スピード）でスピードに与える代表重み。M1 で生スピードが最強の
# 信号だったため、ファンダ上位（A1=28）に準じる強さで束ねて③を見る（最適化ではない代表値）。
SP_WEIGHT = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ============================================================
# スピード・総合スコア の次元ビルダー（builder(a, wr_blend) -> (vals, mask)）
# ============================================================


def _speed_builder(col: str):
    """スピード生値（sp_*）の vals/mask。AUC は順序のみ使うため生値で足りる。"""

    def b(a, wb):
        return a[col], a[f"{col}_m"]

    return b


def _total_builder(weights: dict):
    """固定配点で束ねた総合スコアの vals/mask。スコアは全頭算出のため mask は全 True。"""

    def b(a, wb):
        s = core.score_all(a, weights, wb)
        return s, np.ones(a["N"], dtype=bool)

    return b


SPEED_DIMS = [
    ("SP_best", {"raw": _speed_builder("sp_soha_best")}),
    ("SP_recent", {"raw": _speed_builder("sp_soha_recent")}),
    ("SP_avg", {"raw": _speed_builder("sp_soha_avg")}),
    ("SP_ato", {"raw": _speed_builder("sp_ato3f_best")}),
]

_NAMES_EXT = {
    **_NAMES,
    "SP_best": "走破best",
    "SP_recent": "走破直近",
    "SP_avg": "走破平均",
    "SP_ato": "上り3Fbest",
    "TOTAL_FUND": "総合(ファンダ)",
    "TOTAL_FS": "総合(ファンダ+速)",
}


def build_dims_general() -> list:
    """ファンダ14次元＋スピード4変種＋総合スコア2変種の順序付きリストを返す。"""
    dims = list(build_dimensions())
    dims += SPEED_DIMS
    dims.append(("TOTAL_FUND", {"score": _total_builder(DEFAULT_WEIGHTS)}))
    dims.append(
        ("TOTAL_FS", {"score": _total_builder({**DEFAULT_WEIGHTS, "SP": SP_WEIGHT})})
    )
    return dims


def main() -> None:
    """一般戦キャッシュを IS 限定で読み、ファンダ＋スピード＋総合を③診断して保存する。"""
    ap = argparse.ArgumentParser(
        description="Direction A トラックA: 一般戦サブ項目+スピード+総合 信号診断（IS限定・OOS封印）"
    )
    ap.add_argument(
        "--start-year", type=int, default=IS_START, help="解析開始年（既定 IS=1993）"
    )
    ap.add_argument(
        "--end-year", type=int, default=IS_END, help="解析終了年（既定 IS=2013）"
    )
    ap.add_argument(
        "--top-n", type=int, default=3, help="入賞ラベルの順位上限（既定3＝複勝圏）"
    )
    ap.add_argument(
        "--odds-ratio", type=float, default=1.5, help="③オッズ整合の許容比（既定1.5）"
    )
    ap.add_argument(
        "--auc-min",
        type=float,
        default=0.52,
        help="信号ありと見なすAUC下限（既定0.52）",
    )
    ap.add_argument(
        "--wr-blend",
        type=float,
        default=DEFAULT_WR_BLEND,
        help="ブレンド変種の勝率/ROI比（既定0.6）",
    )
    args = ap.parse_args()

    if args.start_year < IS_START or args.end_year > IS_END:
        logger.error(
            f"OOS封印違反: range={args.start_year}-{args.end_year} は "
            f"IS={IS_START}-{IS_END} 外です。IS内に限定せよ。"
        )
        sys.exit(1)
    if args.start_year > args.end_year:
        logger.error(
            f"期間指定が不正です: start-year={args.start_year} > end-year={args.end_year}"
        )
        sys.exit(1)

    db_path = _BACKEND_DIR / "bloodline.db"
    if not db_path.exists():
        logger.error(f"DBファイルが見つかりません: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # OOS を SQL で封印（year_max=IS_END）。一般戦キャッシュはスピード列も読む。
        a = core.load_arrays(conn, table=CACHE_GENERAL, year_max=IS_END)
    finally:
        conn.close()
    if a["N"] == 0:
        logger.error(
            f"{CACHE_GENERAL} が空です。先に precompute_general.py を実行してください。"
        )
        sys.exit(1)
    a["_range"] = (args.start_year, args.end_year)
    logger.info(f"対象（一般戦・IS≤{IS_END}）: {a['N']:,} 頭 / {a['R']:,} レース")

    report = analyze(
        a,
        args.top_n,
        args.odds_ratio,
        args.auc_min,
        args.wr_blend,
        dims=build_dims_general(),
        names=_NAMES_EXT,
    )
    report["sp_weight"] = SP_WEIGHT
    OUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

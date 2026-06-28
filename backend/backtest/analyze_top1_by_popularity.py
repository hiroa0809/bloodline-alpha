"""Direction A 追補: システム順位Top-1を「その馬の人気(オッズ順位)」で層別した単勝ROI診断。

前テスト（analyze_dangerous_favorites）の**逆向き**。前回は「1番人気をシステム順位で
層別」して市場の人気馬の信頼度を測った。本診断は軸を入れ替え、**常にシステムTop-1を単勝で
買い、その馬の人気(オッズ順位)別にROIを見る**。

狙い: システムが市場より高く評価した馬（Top-1が2〜6番人気＝我々が市場に逆らって推す
「逆張りピック」）に妙味があるか、人気が下がる(=市場との乖離が広がる)ほどROIが開くかを測る。
人気1（Top-1＝1番人気＝市場と一致）は前テスト/flat-top1 で既知のため、層別表では参考行に留める。

仮説の見立て: これまでの全診断（ファンダ単独・スピード）でファンダ予測力は市場に織り込み済み
（増分優位なし≈互角）と確証済み。逆張りピックも市場が正しく割り引いた馬を拾うだけなら、人気が
下がるほどROIは悪化する（=妙味なし）と予想される。本診断はそれを順位別に直接確認する。

金庫ルール（CLAUDE.md「バックテスト方法論」）厳守:
  - 探索は IS（1993-2013）限定。OOS は load_arrays(year_max=IS_END) で SQL 封印。
  - スコアは固定（再最適化しない）。新馬戦=DEFAULT_WEIGHTS / 一般戦=top1_weights_general.json。
  - 人気バケット境界は事前登録（結果を見て動かさない）。

使い方:
    python backend/backtest/analyze_top1_by_popularity.py
    python backend/backtest/analyze_top1_by_popularity.py --smoke   # 狭年範囲で実行可否のみ確認
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
from backtest.top1_core import (  # noqa: E402  （PR#12 でレビュー済みの再利用部品）
    CACHE_GENERAL,
    CACHE_TABLE,
    _EPS,
    load_arrays,
    score_all,
)
from backtest.run_backtest import (  # noqa: E402
    DEFAULT_WEIGHTS,
    DEFAULT_WR_BLEND,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "top1_by_popularity_report.json"
GEN_WEIGHTS_PATH = _BACKEND_DIR / "backtest" / "top1_weights_general.json"

# IS（学習区間。CLAUDE.md「バックテスト方法論」）。OOS は封印。
IS_START, IS_END = 1993, 2013
# スモーク用の小窓（IS内・実行可否確認のみ）。
SMOKE_START, SMOKE_END = 2000, 2005

# システムTop-1の人気(オッズ順位)バケット（事前登録）。(下限, 上限, ラベル)。
# 人気1は「Top-1＝1番人気＝市場一致」で既知のため参考行として残すのみ。
POP_BUCKETS = [
    (1, 1, "人気1(参考)"),
    (2, 2, "人気2"),
    (3, 3, "人気3"),
    (4, 4, "人気4"),
    (5, 5, "人気5"),
    (6, 6, "人気6"),
    (7, 99, "人気7+"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _umaban_int(v) -> int:
    """umaban(object/str) を安全に int 化。失敗時は大きい値（同点時に後ろへ回す）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 1 << 30


def collect_top1_records(a: dict, scores: np.ndarray, y0: int, y1: int) -> list[dict]:
    """各レースのシステムTop-1（スコア最大・同点は馬番昇順）の人気/オッズ/着を集める。

    compute_score の argmax 規約（-score, umaban 昇順）に一致させる。Top-1の単勝オッズ欠落・
    人気欠落(ninki<=0)のレースは「買えない」ため除外（被覆に反映）。
    """
    starts = a["race_start"]
    ry = a["race_year"]
    ninki = a["ninki"]
    odds = a["odds"]
    won = a["won"]
    chaku = a["chaku"]
    umaban = a["umaban"]
    records: list[dict] = []
    for r in range(a["R"]):
        if not (y0 <= ry[r] <= y1):
            continue
        lo, hi = int(starts[r]), int(starts[r + 1])
        sl = scores[lo:hi]
        mx = sl.max()
        # 同点1位は馬番昇順で1頭に確定（compute_score の安定ソートと同規約）。
        top = min(
            (i for i in range(lo, hi) if sl[i - lo] >= mx - _EPS),
            key=lambda i: _umaban_int(umaban[i]),
        )
        top_odds = odds[top]
        top_ninki = int(ninki[top])
        if np.isnan(top_odds) or top_odds <= 0 or top_ninki <= 0:
            continue  # 買えない（オッズ/人気欠落）レースは除外
        # 市場含意勝率（1/オッズのレース内正規化＝控除率除去）。Top-1馬の含意勝率。
        inv = 0.0
        for j in range(lo, hi):
            oj = odds[j]
            if not np.isnan(oj) and oj > 0:
                inv += 1.0 / oj
        records.append(
            {
                "ninki": top_ninki,
                "odds": float(top_odds),
                "won": 1 if won[top] == 1 else 0,
                "placed": 1 if 1 <= int(chaku[top]) <= 3 else 0,
                "implied": (1.0 / top_odds) / inv if inv else None,
            }
        )
    return records


def summarize(records: list[dict]) -> dict:
    """Top-1レコード群 → N・勝率・複勝率・単勝ROI・平均含意勝率・平均オッズ。"""
    n = len(records)
    if n == 0:
        return {
            "n": 0,
            "hit": 0.0,
            "place": 0.0,
            "roi": 0.0,
            "implied": 0.0,
            "avg_odds": 0.0,
        }
    wins = sum(r["won"] for r in records)
    plc = sum(r["placed"] for r in records)
    ret = sum(r["odds"] for r in records if r["won"])
    imp = [r["implied"] for r in records if r["implied"] is not None]
    return {
        "n": n,
        "hit": wins / n,
        "place": plc / n,
        "roi": ret / n,
        "implied": sum(imp) / len(imp) if imp else 0.0,
        "avg_odds": sum(r["odds"] for r in records) / n,
    }


def by_popularity(records: list[dict]) -> dict[str, dict]:
    """システムTop-1を人気バケット別に集計。"""
    out: dict[str, dict] = {}
    for lo, hi, label in POP_BUCKETS:
        sub = [r for r in records if lo <= r["ninki"] <= hi]
        out[label] = summarize(sub)
    return out


def _log_summary(label: str, m: dict) -> None:
    logger.info(
        f"  {label:<10}N={m['n']:>6,}  勝率{m['hit'] * 100:>5.1f}%  "
        f"複勝{m['place'] * 100:>5.1f}%  単勝ROI{m['roi'] * 100:>6.1f}%  "
        f"含意勝率{m['implied'] * 100:>5.1f}%  平均{m['avg_odds']:>6.2f}倍"
    )


def load_dataset(conn: sqlite3.Connection, table: str, year_max: int) -> dict:
    """キャッシュ表を読み（OOS封印）、固定スコアでスコア配列を付けて返す。"""
    a = load_arrays(conn, table=table, year_max=year_max)
    if table == CACHE_GENERAL:
        cfg = json.loads(GEN_WEIGHTS_PATH.read_text(encoding="utf-8"))
        weights, blend = cfg["weights"], cfg["wr_blend"]
    else:
        weights, blend = DEFAULT_WEIGHTS, DEFAULT_WR_BLEND
    a["_scores"] = score_all(a, weights, blend)
    return a


def run_for_dataset(a: dict, name: str, y0: int, y1: int) -> dict:
    """1データセット分の診断（全Top-1ベースライン＋人気別層別）。"""
    logger.info(f"=== {name}（{y0}-{y1}・常にシステムTop-1を単勝） ===")
    records = collect_top1_records(a, a["_scores"], y0, y1)
    baseline = summarize(records)
    logger.info("全Top-1（flat-top1・人気フィルタなし）:")
    _log_summary("全Top-1", baseline)
    buckets = by_popularity(records)
    logger.info("システムTop-1の人気(オッズ順位)別 単勝ROI:")
    for _, _, label in POP_BUCKETS:
        _log_summary(label, buckets[label])
    return {"baseline": baseline, "by_popularity": buckets}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="システムTop-1を人気別に層別した単勝ROI診断（IS限定・OOS封印）"
    )
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="IS内小窓で実行可否のみ確認（本番扱いしない）",
    )
    args = ap.parse_args()

    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    y0, y1 = (SMOKE_START, SMOKE_END) if args.smoke else (IS_START, IS_END)
    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    try:
        maiden = load_dataset(conn, CACHE_TABLE, year_max=y1)
        general = load_dataset(conn, CACHE_GENERAL, year_max=y1)
    finally:
        conn.close()

    result = {
        "新馬戦": run_for_dataset(maiden, "新馬戦", y0, y1),
        "一般戦": run_for_dataset(general, "一般戦", y0, y1),
    }

    if args.smoke:
        logger.info("スモーク完了（結果の良し悪しは判断材料にしない）。")
        return

    out = {
        "is_period": [IS_START, IS_END],
        "pop_buckets": [label for _, _, label in POP_BUCKETS],
        "datasets": result,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

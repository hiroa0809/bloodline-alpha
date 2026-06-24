"""#B5 Phase 1: サブ項目ごとの予測力（信号）診断 — 1次元ずつの棚卸し。

#B3〜Stage D は14次元を「同時に」最適化したため、どの項目が本当に予測力を持つか・
各項目の適切な配点方法は何かが組合せの中に埋もれた。本スクリプトは方針転換として
14次元を1個ずつ独立に評価し、予測力の無い次元を削るための診断を行う（フィルタ型）。

Top-1一致率は「1頭・二値」しか見ずノイジー。代わりに全出走馬の並び順を使う
**レース内AUC** を主指標にする＝同一レース内の (入賞馬, 非入賞馬) ペアで「入賞馬の方が
サブスコアが高い」割合。0.5=無力、>0.5=予測力あり。レース内で比べるのでコース・距離・
格・メンバーといったレース文脈は自動的に相殺される。

4指標（すべて IS 限定・OOS 封印）:
  ① レース内AUC（主）   … 削る/残すの一次判定。
  ② 市場参照AUC         … 同じラベルを市場スコア(=1/単勝オッズ)で測る基準線。
  ③ オッズ整合AUC（増分）… ①のペアを「オッズが近い(比≤R)」ペアに限定。市場評価を
                          揃えた上での予測力＝市場を超えるエッジの芽（核心）。
  ④ 五分位リフト         … サブスコア5段階別の入賞率。偏りの可視化・説明用。

金庫ルール（CLAUDE.md「バックテスト方法論」）厳守:
  - 探索は IS（1993-2013）限定。OOS-1〜3 は一切評価しない（封印）。
  - しきい値（top_n / odds_ratio / auc_min）は事前登録。OOS を見て動かさない。
  - 3ブロック一貫ゲート: IS を3分割し、全ブロックで AUC>0.5 が揃う項目だけ「信号あり」
    ＝1時期のまぐれを弾く。

配点方法(b) の変種（キャッシュ内のみ・再前計算なし）を項目ごとに比較し最良を採用:
  単一系(A1,A2,A3,C1,C2,E1,E2): 勝率pctlのみ / 回収率pctlのみ / ブレンド
  B系(B1-B4): 父(sire)のみ / 母父(bms)のみ / 父母父ブレンド
  C3: 馬主のみ / 生産者のみ / 平均
  A4(近交,連続) / A5(アウト,二値): そのまま

使い方:
    python backend/backtest/analyze_subscore_signal.py
    python backend/backtest/analyze_subscore_signal.py --start-year 2000 --end-year 2005  # スモーク
    python backend/backtest/analyze_subscore_signal.py --top-n 5 --odds-ratio 2.0
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

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "subscore_signal_report.json"

# IS（学習区間。CLAUDE.md「バックテスト方法論」）。OOS は封印。
IS_START, IS_END = 1993, 2013
# 3ブロック一貫ゲート（optimize_robust.IS_BLOCKS と同一値。optuna 依存回避のためローカル定義）。
IS_BLOCKS = [(1993, 1999), (2000, 2006), (2007, 2013)]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ============================================================
# 各サブ項目の変種ビルダー: builder(a, wr_blend) -> (vals[float], mask[bool])
# vals は mask=True の要素だけ意味を持つ（mask=False は AUC のペアから除外）。
# ============================================================

_SINGLE = {
    "A1": "a1",
    "A2": "a2",
    "A3": "a3",
    "C1": "c1",
    "C2": "c2",
    "E1": "e1",
    "E2": "e2",
}
_B = {"B1": "b1", "B2": "b2", "B3": "b3", "B4": "b4"}
_NAMES = {
    "A1": "父",
    "A2": "母父",
    "A3": "ニックス",
    "A4": "近交",
    "A5": "アウト",
    "B1": "馬場",
    "B2": "距離",
    "B3": "開催地",
    "B4": "馬場状態",
    "C1": "調教師",
    "C2": "騎手",
    "C3": "馬主/生産者",
    "E1": "枠",
    "E2": "斤量",
}


def _single_builders(k: str) -> dict:
    def wr(a, wb):
        return a[f"{k}_wr"], a[f"{k}_m"]

    def roi(a, wb):
        return a[f"{k}_roi"], a[f"{k}_m"]

    def blend(a, wb):
        m = a[f"{k}_m"]
        return np.where(m, a[f"{k}_wr"] * wb + a[f"{k}_roi"] * (1 - wb), 0.0), m

    return {"wr": wr, "roi": roi, "blend": blend}


def _b_builders(k: str) -> dict:
    def sire(a, wb):
        return core._pair(
            a[f"{k}_sire_wr"], a[f"{k}_sire_roi"], a[f"{k}_sire_m"], wb
        ), a[f"{k}_sire_m"]

    def bms(a, wb):
        return core._pair(a[f"{k}_bms_wr"], a[f"{k}_bms_roi"], a[f"{k}_bms_m"], wb), a[
            f"{k}_bms_m"
        ]

    def blend(a, wb):
        sp = core._pair(a[f"{k}_sire_wr"], a[f"{k}_sire_roi"], a[f"{k}_sire_m"], wb)
        bp = core._pair(a[f"{k}_bms_wr"], a[f"{k}_bms_roi"], a[f"{k}_bms_m"], wb)
        sm, bm = a[f"{k}_sire_m"], a[f"{k}_bms_m"]
        both = sp * core.BMS_BLEND_SIRE + bp * (1.0 - core.BMS_BLEND_SIRE)
        combined = np.where(sm & bm, both, np.where(sm, sp, np.where(bm, bp, 0.0)))
        return combined, (sm | bm)

    return {"sire": sire, "bms": bms, "blend": blend}


def _c3_builders() -> dict:
    def owner(a, wb):
        return core._pair(a["c3_owner_wr"], a["c3_owner_roi"], a["c3_owner_m"], wb), a[
            "c3_owner_m"
        ]

    def breeder(a, wb):
        return core._pair(
            a["c3_breeder_wr"], a["c3_breeder_roi"], a["c3_breeder_m"], wb
        ), a["c3_breeder_m"]

    def avg(a, wb):
        op = core._pair(a["c3_owner_wr"], a["c3_owner_roi"], a["c3_owner_m"], wb)
        brp = core._pair(a["c3_breeder_wr"], a["c3_breeder_roi"], a["c3_breeder_m"], wb)
        return (op + brp) * 0.5, (a["c3_owner_m"] | a["c3_breeder_m"])

    return {"owner": owner, "breeder": breeder, "avg": avg}


def _a4_builder(a, wb):
    return a["a4_col"], ~np.isnan(a["a4_coi"])


def _a5_builder(a, wb):
    return a["a5_col"], ~np.isnan(a["a5_outbreed"])


def build_dimensions() -> list[tuple[str, dict]]:
    """順序付きの (sub_label, {variant: builder}) リスト（14次元）。"""
    dims: list[tuple[str, dict]] = []
    for sub in ("A1", "A2", "A3"):
        dims.append((sub, _single_builders(_SINGLE[sub])))
    dims.append(("A4", {"coi": _a4_builder}))
    dims.append(("A5", {"outbreed": _a5_builder}))
    for sub in ("B1", "B2", "B3", "B4"):
        dims.append((sub, _b_builders(_B[sub])))
    for sub in ("C1", "C2"):
        dims.append((sub, _single_builders(_SINGLE[sub])))
    dims.append(("C3", _c3_builders()))
    for sub in ("E1", "E2"):
        dims.append((sub, _single_builders(_SINGLE[sub])))
    return dims


# ============================================================
# レース内AUC エンジン
# ============================================================


def race_indices(a: dict, y_start: int, y_end: int) -> list[tuple[int, int]]:
    """年範囲 [y_start, y_end] に入る各レースの頭スライス (h0, h1) を返す。"""
    ry = a["race_year"]
    starts = a["race_start"]
    r0 = int(np.searchsorted(ry, y_start, "left"))
    r1 = int(np.searchsorted(ry, y_end, "right"))
    return [(int(starts[r]), int(starts[r + 1])) for r in range(r0, r1)]


def auc_over_races(
    a: dict,
    vals: np.ndarray,
    mask: np.ndarray,
    idx: list[tuple[int, int]],
    top_n: int,
    odds_ratio: float,
) -> dict:
    """レース内AUC（①）とオッズ整合AUC（③）をペア加重でプール集計する。

    各レースで「データ有り(mask) かつ 有効着(chaku>=1)」の馬だけを使い、(入賞=TOP-N,
    非入賞) の全ペアで concordant（入賞馬の vals が高い、同値は0.5）を数える。AUC は
    concordant 合計 / 総ペア数。③は各ペアを「単勝オッズ比 ≤ odds_ratio」に限定する。
    """
    chaku = a["chaku"]
    odds = a["odds"]
    conc = pairs = 0.0
    mconc = 0.0
    mpairs = 0.0
    for h0, h1 in idx:
        m = mask[h0:h1]
        c = chaku[h0:h1]
        valid = m & (c >= 1)
        nv = int(valid.sum())
        if nv < 2:
            continue
        v = vals[h0:h1][valid]
        cc = c[valid]
        pos = cc <= top_n
        npos = int(pos.sum())
        nneg = nv - npos
        if npos == 0 or nneg == 0:
            continue
        vp = v[pos][:, None]
        vn = v[~pos][None, :]
        diff = vp - vn
        cmat = (diff > 0).astype(np.float64) + 0.5 * (diff == 0)
        conc += float(cmat.sum())
        pairs += npos * nneg
        # ③ オッズ整合: オッズが近いペアだけ
        o = odds[h0:h1][valid]
        op = o[pos][:, None]
        on = o[~pos][None, :]
        ovalid = (op > 0) & (on > 0)
        mn = np.minimum(op, on)
        # ovalid=False の所は分母を1に置換し div0 警告を回避（結果は np.where で inf に上書き）。
        ratio = np.where(ovalid, np.maximum(op, on) / np.where(ovalid, mn, 1.0), np.inf)
        fmask = ratio <= odds_ratio
        mconc += float((cmat * fmask).sum())
        mpairs += float(fmask.sum())
    return {
        "auc": conc / pairs if pairs else float("nan"),
        "pairs": int(pairs),
        "matched_auc": mconc / mpairs if mpairs else float("nan"),
        "matched_pairs": int(mpairs),
    }


def auc_only(a, vals, mask, idx, top_n) -> float:
    """①AUC だけを返す軽量版（TOP-N 感度の併記用）。"""
    chaku = a["chaku"]
    conc = pairs = 0.0
    for h0, h1 in idx:
        m = mask[h0:h1]
        c = chaku[h0:h1]
        valid = m & (c >= 1)
        if int(valid.sum()) < 2:
            continue
        v = vals[h0:h1][valid]
        pos = c[valid] <= top_n
        npos = int(pos.sum())
        nneg = int(valid.sum()) - npos
        if npos == 0 or nneg == 0:
            continue
        diff = v[pos][:, None] - v[~pos][None, :]
        conc += float((diff > 0).sum()) + 0.5 * float((diff == 0).sum())
        pairs += npos * nneg
    return conc / pairs if pairs else float("nan")


def quintile_lift(a, vals, mask, idx, top_n) -> list[dict]:
    """最良変種の五分位別入賞率（プール）。高サブスコア帯ほど入賞が偏るかの可視化。"""
    chaku = a["chaku"]
    vs: list[float] = []
    ps: list[int] = []
    for h0, h1 in idx:
        m = mask[h0:h1]
        c = chaku[h0:h1]
        valid = m & (c >= 1)
        if not valid.any():
            continue
        vs.extend(vals[h0:h1][valid].tolist())
        ps.extend((c[valid] <= top_n).astype(int).tolist())
    if len(vs) < 5:
        return []
    v = np.array(vs)
    p = np.array(ps)
    edges = np.quantile(v, [0.2, 0.4, 0.6, 0.8])
    bins = np.digitize(v, edges)  # 0..4
    out = []
    for b in range(5):
        sel = bins == b
        n = int(sel.sum())
        out.append(
            {
                "quintile": b + 1,
                "n": n,
                "top_n_rate": float(p[sel].mean()) if n else 0.0,
            }
        )
    return out


def market_builder(a):
    """市場スコア = 1/単勝オッズ（人気＝高スコア）。mask は有効オッズ。"""
    o = a["odds"]
    m = (~np.isnan(o)) & (o > 0)
    return np.where(m, 1.0 / np.where(m, o, 1.0), 0.0), m


# ============================================================
# メイン
# ============================================================


def analyze(
    a: dict, top_n: int, odds_ratio: float, auc_min: float, wr_blend: float
) -> dict:
    """全14次元×変種を評価し、判定ラベル付きのレポート dict を返す。"""
    y0, y1 = a["_range"]
    pooled_idx = race_indices(a, y0, y1)
    block_idx = [
        (b, race_indices(a, b[0], b[1])) for b in IS_BLOCKS if y0 <= b[0] and b[1] <= y1
    ]

    # ② 市場参照（dim 非依存）
    mvals, mmask = market_builder(a)
    market_pooled = auc_over_races(a, mvals, mmask, pooled_idx, top_n, odds_ratio)[
        "auc"
    ]
    market_blocks = [
        {
            "block": list(b),
            "auc": auc_over_races(a, mvals, mmask, idx, top_n, odds_ratio)["auc"],
        }
        for b, idx in block_idx
    ]
    logger.info(
        f"市場参照AUC（1/オッズ・TOP{top_n}）: プール {market_pooled:.4f} / "
        f"ブロック {[round(x['auc'], 3) for x in market_blocks]}"
    )

    results = []
    for sub, variants in build_dimensions():
        vres = {}
        for vname, builder in variants.items():
            vals, mask = builder(a, wr_blend)
            pooled = auc_over_races(a, vals, mask, pooled_idx, top_n, odds_ratio)
            blocks = [
                {
                    "block": list(b),
                    **auc_over_races(a, vals, mask, idx, top_n, odds_ratio),
                }
                for b, idx in block_idx
            ]
            vres[vname] = {**pooled, "block_aucs": blocks}
        # 最良変種＝プール①AUC 最大（nan は除外）
        best_v = max(
            vres,
            key=lambda v: vres[v]["auc"] if not np.isnan(vres[v]["auc"]) else -1.0,
        )
        best = vres[best_v]
        block_aucs = [b["auc"] for b in best["block_aucs"]]
        block_maucs = [b["matched_auc"] for b in best["block_aucs"]]
        consistent = bool(block_aucs) and all(
            (not np.isnan(x)) and x > 0.5 for x in block_aucs
        )
        matched_consistent = bool(block_maucs) and all(
            (not np.isnan(x)) and x > 0.5 for x in block_maucs
        )
        # 判定
        if np.isnan(best["auc"]) or best["auc"] < auc_min or not consistent:
            label = "削除候補"
        elif (
            (not np.isnan(best["matched_auc"]))
            and best["matched_auc"] > auc_min
            and matched_consistent
        ):
            label = "エッジ候補"
        else:
            label = "市場超えなし(残す)"

        vals, mask = variants[best_v](a, wr_blend)
        results.append(
            {
                "sub": sub,
                "name": _NAMES[sub],
                "best_variant": best_v,
                "label": label,
                "consistent": consistent,
                "matched_consistent": matched_consistent,
                "pooled_auc": best["auc"],
                "market_auc": market_pooled,
                "matched_auc": best["matched_auc"],
                "matched_pairs": best["matched_pairs"],
                "auc_top1": auc_only(a, vals, mask, pooled_idx, 1),
                "auc_top5": auc_only(a, vals, mask, pooled_idx, 5),
                "quintile_lift": quintile_lift(a, vals, mask, pooled_idx, top_n),
                "variants": vres,
            }
        )

    results.sort(
        key=lambda r: r["pooled_auc"] if not np.isnan(r["pooled_auc"]) else -1.0,
        reverse=True,
    )
    _log_table(results, top_n)
    return {
        "is_period": [y0, y1],
        "top_n": top_n,
        "odds_ratio": odds_ratio,
        "auc_min": auc_min,
        "wr_blend": wr_blend,
        "is_blocks": IS_BLOCKS,
        "market_reference": {"pooled_auc": market_pooled, "blocks": market_blocks},
        "dimensions": results,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _log_table(results: list[dict], top_n: int) -> None:
    """①AUC 降順のサマリー表をログ出力。"""
    logger.info(f"=== サブ項目 信号診断（TOP{top_n} 入賞・①AUC降順） ===")
    logger.info(
        f"  {'項目':<6}{'最良変種':<8}{'①AUC':>8}{'市場②':>8}{'③整合':>8}"
        f"{'一貫':>5}  判定"
    )
    for r in results:
        mauc = "—" if np.isnan(r["matched_auc"]) else f"{r['matched_auc']:.3f}"
        logger.info(
            f"  {r['sub']:<6}{r['best_variant']:<8}{r['pooled_auc']:>8.3f}"
            f"{r['market_auc']:>8.3f}{mauc:>8}{'○' if r['consistent'] else '×':>5}"
            f"  {r['label']}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="#B5 Phase1 サブ項目 信号診断（IS限定・OOS封印）"
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
        "--odds-ratio",
        type=float,
        default=1.5,
        help="③オッズ整合の許容オッズ比（既定1.5）",
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
        default=0.6,
        help="ブレンド変種の勝率/ROI比（既定0.6）",
    )
    args = ap.parse_args()

    if args.end_year > IS_END:
        logger.error(
            f"OOS封印違反: end-year={args.end_year} > IS_END={IS_END}。IS内に限定せよ。"
        )
        sys.exit(1)
    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    try:
        a = core.load_arrays(conn)
    finally:
        conn.close()
    a["_range"] = (args.start_year, args.end_year)

    report = analyze(a, args.top_n, args.odds_ratio, args.auc_min, args.wr_blend)
    OUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

"""Direction A 第1マイルストーン: 一般戦のスピード指標 信号診断（go/no-go ゲート）。

新馬戦のファンダ（血統A＋条件B＋人的C＋斤量E）は pick/fade とも市場に織り込まれ済み
（≈互角）と確証された（#B5 一連）。一方、億単位の利益を上げた実在の馬券システム（卍氏）は
JRA-VAN（＝本プロジェクトと同一データ源）で全レースを網羅し、**過去走タイム＝スピード指標**
を核にエッジを突いていた。本スクリプトは Direction A（一般戦＋スピードへの転換）の第1歩
として「**一般戦のスピード指標に“市場を超える増分エッジ”が今も残っているか**」だけを安く
検証する go/no-go ゲートである（通過なら本格スピード図表・エッジベット運用へ。不通過なら
大改修を止める）。

仮説と検証: スピードは生の予測力が強い特徴。だが市場も当然それを織り込む。重要なのは
「市場（オッズ）を揃えた上での増分（③オッズ整合AUC）」が残るか。信号診断
（analyze_subscore_signal）の AUC 機構をそのまま一般戦×スピードへ適用する。

金庫ルール厳守:
  - 対象＝非新馬の平地戦（障害除外）・IS(1993-2013)限定・OOS(2014+)封印。
  - point-in-time: 馬の過去走は対象レース日より厳密に前、標準タイムは対象年より前の年のみ。
  - しきい値（top_n / odds_ratio / auc_min）は事前登録。既存ライブスコアには触れない。

スピード as-of 特徴量:
  走破タイムを (競馬場×馬場種別×距離) の as-of 標準タイム（対象年“より前”の中央値）で
  基準化＝スピード値（速いほど高）。馬ごとに過去走スピード値を best/recent/avg で集約
  （変種比較）。上がり3F版(best)も補助で併記。馬場差/ペース/クラス補正は次マイルストーンへ。

使い方:
    python backend/backtest/analyze_speed_signal.py
    python backend/backtest/analyze_speed_signal.py --smoke        # 小窓・実行可否のみ
    python backend/backtest/analyze_speed_signal.py --top-n 5 --odds-ratio 2.0
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
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
from backtest.asof_helpers import track_to_surface  # noqa: E402

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "speed_signal_report.json"

# クリーン期間の開始（ウォームアップ＝過去走履歴の蓄積に使用。CLAUDE.md 方法論）。
SCAN_START = 1986
# スモーク用の小窓（実行可否のみ確認）。
SMOKE_SCAN_START, SMOKE_ANA_START, SMOKE_ANA_END = 2005, 2008, 2010

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ============================================================
# タイム文字列のパース
# ============================================================


def parse_soha(s: str | None) -> float | None:
    """走破タイム文字列 → 秒。"MSSt"（分/秒2桁/0.1秒）。例 '1103'→70.3, '0573'→57.3。"""
    if not s:
        return None
    s = s.strip()
    if not s.isdigit() or len(s) < 3:
        return None
    tenths = int(s[-1])
    sec = int(s[-3:-1])
    mins = int(s[:-3]) if len(s) > 3 else 0
    if sec >= 60:
        return None
    total = mins * 60 + sec + tenths / 10.0
    return total if total > 0 else None


def parse_ato3f(s: str | None) -> float | None:
    """上がり3F文字列 → 秒。"SSt"（秒/0.1秒）。例 '345'→34.5。"""
    if not s:
        return None
    s = s.strip()
    if not s.isdigit() or len(s) < 2:
        return None
    total = int(s[:-1]) + int(s[-1]) / 10.0
    return total if total > 0 else None


def _safe_chaku(v) -> int:
    """着順TEXT → int（>=1 が有効着、それ以外0）。top1_core.chk と同方針。"""
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return 0
    return iv if iv >= 1 else 0


# ============================================================
# データロード（レース条件 + 出走行）
# ============================================================


def load_race_info(conn: sqlite3.Connection, scan_start: int, ana_end: int) -> dict:
    """jvd_race → レースPK → {date, year, keibajo, surface, kyori, is_maiden}。

    年範囲は SQL 側で絞り、OOS（ana_end 超）行をアプリへ読み込まない（封印を境界で担保）。
    """
    info: dict[tuple, dict] = {}
    cur = conn.execute(
        "SELECT kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, "
        "  race_bango, track_code, kyori, "
        "  kyoso_joken_code_2sai, kyoso_joken_code_3sai FROM jvd_race "
        "WHERE CAST(kaisai_nen AS INTEGER) BETWEEN ? AND ?",
        (scan_start, ana_end),
    )
    for nen, tsukihi, keibajo, kai, nichime, rbango, track, kyori, j2, j3 in cur:
        try:
            year = int(nen)
        except (ValueError, TypeError):
            continue
        surface = track_to_surface(track)  # 障害は None
        info[(nen, tsukihi, keibajo, kai, nichime, rbango)] = {
            "date": f"{nen}{tsukihi}",
            "year": year,
            "keibajo": keibajo,
            "surface": surface,
            "kyori": (kyori or "").strip() or None,
            "is_maiden": "701" in (j2, j3),
        }
    return info


def load_runs(conn: sqlite3.Connection, race_info: dict, scan_start: int, ana_end: int):
    """jvd_race_uma を走査し、平地戦の出走行を収集する（year<=ana_end＝OOS封印）。

    返却: runs（list[dict]・date昇順未ソート）。各 dict は速度値算出に必要な素データを持つ。
    """
    runs: list[dict] = []
    cur = conn.execute(
        "SELECT kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, "
        "  race_bango, umaban, ketto_toroku_bango, kakutei_chakujun, tansho_odds, "
        "  ijo_kubun_code, soha_time, ato_3f_time FROM jvd_race_uma "
        "WHERE CAST(kaisai_nen AS INTEGER) BETWEEN ? AND ?",
        (scan_start, ana_end),
    )
    for row in cur:
        (
            nen,
            tsukihi,
            keibajo,
            kai,
            nichime,
            rbango,
            umaban,
            ketto,
            chaku,
            odds,
            ijo,
            soha,
            ato3f,
        ) = row
        info = race_info.get((nen, tsukihi, keibajo, kai, nichime, rbango))
        if info is None or info["surface"] is None:
            continue  # 障害・条件不明は対象外
        year = info["year"]
        if not (scan_start <= year <= ana_end):
            continue  # OOS封印＋ウォームアップ前は無視
        if not (umaban and umaban.strip()):
            continue
        try:
            odds_real = int(odds) / 10.0 if odds and odds.strip() else None
        except (ValueError, TypeError):
            odds_real = None
        chaku_i = _safe_chaku(chaku)
        valid_finish = chaku_i >= 1 and (ijo is None or ijo == "0")
        runs.append(
            {
                "date": info["date"],
                "year": year,
                "race_id": f"{nen}{tsukihi}{keibajo}{kai}{nichime}{rbango}",
                "umaban": umaban.strip(),
                "ketto": ketto,
                "chaku": chaku_i,
                "odds": odds_real,
                "is_maiden": info["is_maiden"],
                "key": (keibajo, info["surface"], info["kyori"]),
                "soha": parse_soha(soha) if valid_finish else None,
                "ato3f": parse_ato3f(ato3f) if valid_finish else None,
            }
        )
    return runs


# ============================================================
# as-of 標準タイム → スピード値 → 馬ごと過去走集約
# ============================================================


def build_standards(runs: list[dict], field: str) -> dict:
    """(key,year)→標準タイム（その年“より前”の完走中央値）。field='soha'|'ato3f'。"""
    by_key_year: dict[tuple, dict[int, list[float]]] = {}
    for r in runs:
        t = r[field]
        if t is None:
            continue
        by_key_year.setdefault(r["key"], {}).setdefault(r["year"], []).append(t)
    standard: dict[tuple, dict[int, float]] = {}
    for key, year_map in by_key_year.items():
        acc: list[float] = []
        std_for_year: dict[int, float] = {}
        for y in sorted(year_map):
            # y より前の年の累積中央値を y の標準とする（未来リークゼロ）。
            if acc:
                std_for_year[y] = statistics.median(acc)
            acc.extend(year_map[y])
        standard[key] = std_for_year
    return standard


def assign_speed_values(runs: list[dict]) -> None:
    """各 run に soha/ato3f のスピード値（標準−自タイム・速いほど高）を付与する。"""
    std_soha = build_standards(runs, "soha")
    std_ato = build_standards(runs, "ato3f")
    for r in runs:
        sv = None
        if r["soha"] is not None:
            base = std_soha.get(r["key"], {}).get(r["year"])
            if base is not None:
                sv = base - r["soha"]
        r["sv_soha"] = sv
        av = None
        if r["ato3f"] is not None:
            base = std_ato.get(r["key"], {}).get(r["year"])
            if base is not None:
                av = base - r["ato3f"]
        r["sv_ato3f"] = av


def assign_horse_features(runs: list[dict]) -> None:
    """date昇順で各 run に「自分より前の過去走」スピード値の集約を付与する（strictly-prior）。

    best/recent/avg(soha) と best(ato3f)。過去走ゼロは NaN（AUCペアから除外）。
    """
    runs.sort(key=lambda r: (r["date"], r["race_id"], r["umaban"]))
    # ketto → [best_soha, sum_soha, cnt_soha, last_soha, best_ato]
    agg: dict[str, list[float]] = {}
    for r in runs:
        st = agg.get(r["ketto"])
        if st is None or st[2] == 0:
            r["f_best"] = np.nan
            r["f_recent"] = np.nan
            r["f_avg"] = np.nan
        else:
            r["f_best"] = st[0]
            r["f_recent"] = st[3]
            r["f_avg"] = st[1] / st[2]
        r["f_ato_best"] = (
            (st[4] if (st is not None and not np.isnan(st[4])) else np.nan)
            if st is not None
            else np.nan
        )
        # 自分の値で集約を更新（次走以降の過去走になる）
        if st is None:
            st = [np.nan, 0.0, 0, np.nan, np.nan]
            agg[r["ketto"]] = st
        sv = r["sv_soha"]
        if sv is not None:
            st[0] = sv if np.isnan(st[0]) else max(st[0], sv)
            st[1] += sv
            st[2] += 1
            st[3] = sv
        av = r["sv_ato3f"]
        if av is not None:
            st[4] = av if np.isnan(st[4]) else max(st[4], av)


# ============================================================
# 解析用 `a` 配列の構築（analyze_subscore_signal の AUC 機構が要求する構造）
# ============================================================

FEATURES = [
    ("soha_best", "f_best", "走破best"),
    ("soha_recent", "f_recent", "走破直近"),
    ("soha_avg", "f_avg", "走破平均"),
    ("ato3f_best", "f_ato_best", "上り3Fbest"),
]


def build_arrays(runs: list[dict], ana_end: int) -> dict:
    """非新馬・平地の対象 run を (year, race_id, umaban) 昇順に並べ numpy 配列群へ。"""
    targets = [r for r in runs if not r["is_maiden"] and r["year"] <= ana_end]
    targets.sort(key=lambda r: (r["year"], r["race_id"], r["umaban"]))
    n = len(targets)
    a: dict = {}
    a["chaku"] = np.array([r["chaku"] for r in targets], dtype=np.int64)
    a["odds"] = np.array(
        [np.nan if r["odds"] is None else float(r["odds"]) for r in targets],
        dtype=np.float64,
    )
    for fname, fkey, _ in FEATURES:
        vals = np.array([float(r[fkey]) for r in targets], dtype=np.float64)
        a[fname] = vals
        a[f"{fname}_m"] = ~np.isnan(vals)
    # レース境界（race_start: R+1, race_year: R）。year昇順なので年範囲は連続スライス。
    if n == 0:
        a["race_start"] = np.array([0], dtype=np.int64)
        a["race_year"] = np.array([], dtype=np.int64)
        return a
    rid = np.array([r["race_id"] for r in targets], dtype=object)
    year = np.array([r["year"] for r in targets], dtype=np.int64)
    change = (year[1:] != year[:-1]) | (rid[1:] != rid[:-1])
    starts = np.concatenate(([0], np.nonzero(change)[0] + 1, [n])).astype(np.int64)
    a["race_start"] = starts
    a["race_year"] = year[starts[:-1]]
    logger.info(f"  対象（非新馬・平地）: {n:,} 頭 / {len(starts) - 1:,} レース")
    return a


# ============================================================
# 解析
# ============================================================


def analyze(
    a: dict, ana_start: int, ana_end: int, top_n: int, odds_ratio: float, auc_min: float
) -> dict:
    """全スピード変種を①②③④で評価し、go/no-go 判定付きレポートを返す。"""
    pooled_idx = race_indices(a, ana_start, ana_end)
    blocks = _consistency_blocks(ana_start, ana_end)
    block_idx = [(b, race_indices(a, b[0], b[1])) for b in blocks]

    mvals, mmask = market_builder(a)
    market_pooled = auc_over_races(a, mvals, mmask, pooled_idx, top_n, odds_ratio)[
        "auc"
    ]
    logger.info(f"市場参照AUC（1/オッズ・TOP{top_n}）: {market_pooled:.4f}")

    results = []
    for fname, _, label in FEATURES:
        vals, mask = a[fname], a[f"{fname}_m"]
        pooled = auc_over_races(a, vals, mask, pooled_idx, top_n, odds_ratio)
        bstats = [
            {"block": list(b), **auc_over_races(a, vals, mask, idx, top_n, odds_ratio)}
            for b, idx in block_idx
        ]
        block_aucs = [x["auc"] for x in bstats]
        block_maucs = [x["matched_auc"] for x in bstats]
        consistent = bool(block_aucs) and all(
            (not np.isnan(x)) and x > 0.5 for x in block_aucs
        )
        # ③ ゲート: 全ブロックで matched_auc > auc_min（市場制御後の増分が一貫）。
        matched_consistent = bool(block_maucs) and all(
            (not np.isnan(x)) and x > auc_min for x in block_maucs
        )
        if np.isnan(pooled["auc"]) or pooled["auc"] < 0.5 or not consistent:
            verdict = "予測力なし"
        elif matched_consistent:
            verdict = "エッジ候補"
        else:
            verdict = "市場超えなし"
        results.append(
            {
                "feature": fname,
                "name": label,
                "verdict": verdict,
                "pooled_auc": pooled["auc"],
                "market_auc": market_pooled,
                "matched_auc": pooled["matched_auc"],
                "matched_pairs": pooled["matched_pairs"],
                "consistent": consistent,
                "matched_consistent": matched_consistent,
                "auc_top1": auc_only(a, vals, mask, pooled_idx, 1),
                "block_aucs": block_aucs,
                "block_matched_aucs": block_maucs,
                "quintile_lift": quintile_lift(a, vals, mask, pooled_idx, top_n),
            }
        )

    _log_table(results, top_n)
    # go/no-go: スピード(soha)変種のいずれかが「エッジ候補」判定なら通過。
    # verdict は①生AUC>0.5＆ブロック一貫＆③matched_consistent を全て満たす時のみ「エッジ候補」。
    # matched_consistent 単独だと生AUC不足の特徴でも通過し得るため verdict で判定する。
    soha_edge = [
        r
        for r in results
        if r["feature"].startswith("soha") and r["verdict"] == "エッジ候補"
    ]
    gate_pass = len(soha_edge) > 0
    logger.info(
        f"=== go/no-go ゲート: {'通過（エッジ候補あり）' if gate_pass else '不通過（市場超え増分なし）'} ==="
    )
    if gate_pass:
        logger.info(
            "  → 次マイルストーン（本格スピード図表・as-ofキャッシュ・エッジベット）へ進む候補あり"
        )
    else:
        logger.info("  → スピードも市場に織り込み済み（≈互角）。大改修は止めるのが妥当")
    return {
        "analysis_period": [ana_start, ana_end],
        "is_blocks": [list(b) for b in blocks],
        "top_n": top_n,
        "odds_ratio": odds_ratio,
        "auc_min": auc_min,
        "market_reference_auc": market_pooled,
        "features": results,
        "gate_pass": gate_pass,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _log_table(results: list[dict], top_n: int) -> None:
    """①AUC降順サマリーをログ出力。"""
    logger.info(f"=== スピード信号診断（TOP{top_n}入賞・①AUC） ===")
    logger.info(f"  {'特徴':<12}{'①AUC':>8}{'市場②':>8}{'③整合':>8}{'一貫':>5}  判定")
    for r in sorted(
        results,
        key=lambda x: x["pooled_auc"] if not np.isnan(x["pooled_auc"]) else -1,
        reverse=True,
    ):
        mauc = "—" if np.isnan(r["matched_auc"]) else f"{r['matched_auc']:.3f}"
        logger.info(
            f"  {r['name']:<12}{r['pooled_auc']:>8.3f}{r['market_auc']:>8.3f}"
            f"{mauc:>8}{'○' if r['consistent'] else '×':>5}  {r['verdict']}"
        )


def main() -> None:
    """スピード as-of 特徴量を構築し IS 限定で信号診断（go/no-go）を実行する。"""
    ap = argparse.ArgumentParser(
        description="Direction A M1: 一般戦スピード信号診断（IS限定・OOS封印）"
    )
    ap.add_argument("--top-n", type=int, default=3, help="入賞ラベル順位上限（既定3）")
    ap.add_argument(
        "--odds-ratio", type=float, default=1.5, help="③オッズ整合の許容比（既定1.5）"
    )
    ap.add_argument(
        "--auc-min", type=float, default=0.52, help="エッジ判定のAUC下限（既定0.52）"
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

    assign_speed_values(runs)
    assign_horse_features(runs)
    a = build_arrays(runs, ana_end)

    report = analyze(a, ana_start, ana_end, args.top_n, args.odds_ratio, args.auc_min)
    if args.smoke:
        logger.info("スモーク完了（結果の良し悪しは判断材料にしない）。")
        return
    OUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

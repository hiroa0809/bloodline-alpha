"""#B5 危険な人気馬（過剰人気）検出診断 + IS内ウォークフォワードCV検証。

これまでの全診断（#B3〜Stage D・サブ項目信号診断 Phase1）は「当てにいく＝買う側
（pick）」だけを測り、ファンダ（血統A＋条件B＋人的C＋斤量E）の予測力は確定オッズに完全に
織り込まれている（市場超え増分なし≈互角）と確証した。本スクリプトはその**裏面（fade側）**
＝プロジェクト本来の目的「危険な人気馬の特定」を初めて直接検証する。

仮説: 市場が人気を作る論理（騎手・厩舎・血統名）と我々のスコアは相関するため pick では
勝てないが、「**市場は高く評価するのに我々のスコアは低い**」ミスマッチ馬（血統名ハイプ等で
実力以上に売れた馬）が体系的に負けやすいなら、予測力ではなく市場の行動バイアスを突く別の
勝ち筋になりうる。これを既存データだけで安価に測る（新規インポート不要）。

金庫ルール（CLAUDE.md「バックテスト方法論」）厳守:
  - 探索は IS（1993-2013）限定。OOS-1〜3 は一切評価しない（封印）。
  - スコア重みは固定（DEFAULT_WEIGHTS / wr_blend=0.6）。再最適化しない＝#B3の過学習を
    上塗りしない。本診断はスコアの上に乗る「ベットフィルタ/警告層」だけを検証する。
  - 危険判定のファンダ順位閾値（FUND_RANK_THRESHOLDS）・オッズ帯・3ブロックは事前登録。

2部構成:
  Part 1 診断 — IS全体で1番人気を「レース内ファンダ順位」で層別し勝率/複勝率/ROI/実勝率vs
    市場含意勝率を比較。オッズ帯を固定した上での agree vs dangerous 比較で「市場超え増分」を
    判定（[feedback_precise_market_framing] 準拠）。3ブロック一貫ゲートでまぐれを排除。
  Part 2 IS内CV — AVOID（危険な1番人気のレースを避ける）/ FADE（危険なら2番人気に賭ける）を
    事前登録閾値グリッドから学習区間で選び、未見の検証区間で採点。被覆加重の検証ROI＝過学習
    補正後の実力見積もり。

使い方:
    python backend/backtest/analyze_dangerous_favorites.py
    python backend/backtest/analyze_dangerous_favorites.py --wr-blend 0.5 --min-n 50
    python backend/backtest/analyze_dangerous_favorites.py --smoke   # 実行可否のみ（CV省略）
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from backtest.analyze_odds_bands import (  # noqa: E402
    CV_FOLDS,
    ODDS_BANDS,
    _metrics,
    band_of,
)
from backtest.analyze_subscore_signal import IS_BLOCKS  # noqa: E402
from backtest.run_backtest import (  # noqa: E402
    DEFAULT_WEIGHTS,
    DEFAULT_WR_BLEND,
    _safe_int,
    compute_score,
    load_races,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "dangerous_favorite_report.json"

# IS（学習区間。CLAUDE.md「バックテスト方法論」）。OOS は封印。
IS_START, IS_END = 1993, 2013
# スモーク用の小窓（IS内・実行可否確認のみ）。
SMOKE_START, SMOKE_END = 2000, 2005

# 危険判定のファンダ順位閾値グリッド（事前登録）。1番人気のレース内スコア順位が t 以上
# （＝下位）なら「危険＝市場は高評価／我々は低評価」と見なす。Part2 のCVが学習側から選ぶ。
FUND_RANK_THRESHOLDS = [2, 3, 4]
# Part1 表示用の代表閾値（dangerous = fund_rank >= DANGER_RANK）。
DANGER_RANK = 3
# Part1 #2 のファンダ順位バケット (下限, 上限, ラベル)。
RANK_BUCKETS = [(1, 1, "rank1"), (2, 3, "rank2-3"), (4, 99, "rank4+")]

BAND_LABELS = [label for _, _, label in ODDS_BANDS]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _placed(chakujun) -> int:
    """着順TEXT → 複勝圏(1-3着)なら1。非数値（取消/除外）・4着以下は0。"""
    v = _safe_int(chakujun)  # 失敗時0 → 圏外扱い
    return 1 if 1 <= v <= 3 else 0


def collect_race_records(
    races: dict[str, list[sqlite3.Row]],
    y0: int,
    y1: int,
    weights: dict,
    wr_blend: float,
) -> list[dict]:
    """各レースの1番人気成績＋レース内ファンダ順位、および2番人気成績を集める。

    1番人気不在 or その単勝オッズ欠落のレースは除外（run_backtest の fav 集計と同基準＝
    サニティ突合のため）。fav_rank は compute_score 降順（同点は馬番昇順で安定化）での
    1番人気の順位（1=我々も最良＝agree）。
    """
    records: list[dict] = []
    for runners in races.values():
        y = runners[0]["as_of_year"]
        if not (y0 <= y <= y1):
            continue
        fav = next((r for r in runners if r["ninki"] == 1), None)
        if fav is None or fav["tansho_odds"] is None:
            continue

        # レース内ファンダ順位（スコア降順・同点は馬番昇順。compute_score の argmax と同規約）
        ordered = sorted(
            runners,
            key=lambda r: (
                -compute_score(r, weights, wr_blend),
                _safe_int(r["umaban"]),
            ),
        )
        fav_rank = next(i + 1 for i, r in enumerate(ordered) if r is fav)

        # 市場含意勝率（1/オッズのレース内正規化＝控除率を除去）。1番人気の含意勝率。
        inv = [1.0 / float(r["tansho_odds"]) for r in runners if r["tansho_odds"]]
        denom = sum(inv)
        fav_implied = (1.0 / float(fav["tansho_odds"])) / denom if denom else None

        second = next((r for r in runners if r["ninki"] == 2), None)
        sec_odds = (
            float(second["tansho_odds"])
            if second and second["tansho_odds"] is not None
            else None
        )
        records.append(
            {
                "fav_rank": fav_rank,
                "fav_odds": float(fav["tansho_odds"]),
                "fav_won": 1 if fav["won"] == 1 else 0,
                "fav_placed": _placed(fav["chakujun"]),
                "fav_implied": fav_implied,
                "second_odds": sec_odds,
                "second_won": 1 if (second and second["won"] == 1) else 0,
            }
        )
    return records


def fav_summary(records: list[dict]) -> dict:
    """1番人気レコード群 → N・勝率・複勝率・単勝ROI・平均市場含意勝率。"""
    n = len(records)
    if n == 0:
        return {"n": 0, "hit": 0.0, "place": 0.0, "roi": 0.0, "implied": 0.0}
    wins = sum(r["fav_won"] for r in records)
    plc = sum(r["fav_placed"] for r in records)
    ret = sum(r["fav_odds"] for r in records if r["fav_won"])
    imp = [r["fav_implied"] for r in records if r["fav_implied"] is not None]
    return {
        "n": n,
        "hit": wins / n,
        "place": plc / n,
        "roi": ret / n,
        "implied": sum(imp) / len(imp) if imp else 0.0,
    }


def by_rank_bucket(records: list[dict]) -> dict[str, dict]:
    """Part1 #2: 1番人気をレース内ファンダ順位バケット別に集計。"""
    out: dict[str, dict] = {}
    for lo, hi, label in RANK_BUCKETS:
        sub = [r for r in records if lo <= r["fav_rank"] <= hi]
        out[label] = fav_summary(sub)
    return out


def by_band_and_flag(records: list[dict]) -> dict[str, dict]:
    """Part1 #3: オッズ帯を固定し、各帯内で agree(rank1) vs dangerous(rank>=DANGER_RANK)。

    同じオッズ帯（＝市場評価をほぼ揃えた集合）内でも dangerous の勝率/ROIが低ければ
    市場価格を超える増分（[feedback_precise_market_framing]）。差が消えれば増分なし。
    """
    out: dict[str, dict] = {}
    for _, _, band in ODDS_BANDS:
        in_band = [r for r in records if band_of(r["fav_odds"]) == band]
        agree = [r for r in in_band if r["fav_rank"] == 1]
        danger = [r for r in in_band if r["fav_rank"] >= DANGER_RANK]
        out[band] = {
            "agree": fav_summary(agree),
            "dangerous": fav_summary(danger),
        }
    return out


def block_consistency(races: dict, weights: dict, wr_blend: float) -> dict:
    """Part1 #4: 「dangerous の勝率 < agree の勝率」が3ブロック全部で成立するか。"""
    blocks = []
    all_ok = True
    for b0, b1 in IS_BLOCKS:
        recs = collect_race_records(races, b0, b1, weights, wr_blend)
        agree = fav_summary([r for r in recs if r["fav_rank"] == 1])
        danger = fav_summary([r for r in recs if r["fav_rank"] >= DANGER_RANK])
        win_gap = agree["hit"] - danger["hit"]  # >0 なら dangerous の方が負ける
        ok = agree["n"] > 0 and danger["n"] > 0 and win_gap > 0
        all_ok = all_ok and ok
        blocks.append(
            {
                "block": [b0, b1],
                "agree": agree,
                "dangerous": danger,
                "win_gap": win_gap,
                "ok": ok,
            }
        )
    return {"blocks": blocks, "consistent": all_ok}


# ============================================================
# Part 2: 実行可能戦略（事前登録）と IS内CV
# ============================================================


def strat_avoid(records: list[dict], threshold: int) -> dict:
    """AVOID: 1番人気が危険(fund_rank>=threshold)でないレースだけ1番人気に単勝。"""
    sel = [r for r in records if r["fav_rank"] < threshold]
    n = len(sel)
    wins = sum(r["fav_won"] for r in sel)
    ret = sum(r["fav_odds"] for r in sel if r["fav_won"])
    return _metrics(n, wins, ret)


def strat_fade(records: list[dict], threshold: int) -> dict:
    """FADE: 1番人気が危険(fund_rank>=threshold)なレースで2番人気に単勝。

    2番人気のオッズ欠落レースは賭けられないため除外。
    """
    sel = [
        r
        for r in records
        if r["fav_rank"] >= threshold and r["second_odds"] is not None
    ]
    n = len(sel)
    wins = sum(r["second_won"] for r in sel)
    ret = sum(r["second_odds"] for r in sel if r["second_won"])
    return _metrics(n, wins, ret)


def baseline_fav(records: list[dict]) -> dict:
    """素の全1番人気に単勝（フィルタ無し）した N・的中率・ROI。被覆率の分母にも使う。"""
    n = len(records)
    wins = sum(r["fav_won"] for r in records)
    ret = sum(r["fav_odds"] for r in records if r["fav_won"])
    return _metrics(n, wins, ret)


def run_cv_strategy(
    races: dict,
    weights: dict,
    wr_blend: float,
    strat_fn,
    min_n: int,
    name: str,
) -> dict:
    """IS内ウォークフォワードCV。学習で危険閾値を選び未見の検証区間で採点（odds_bands規約）。"""
    logger.info(
        f"--- {name}: 学習で危険閾値選択(候補{FUND_RANK_THRESHOLDS}, 学習N≥{min_n}) "
        f"→ 検証で採点（{len(CV_FOLDS)}フォールド） ---"
    )
    folds = []
    for tr_s, tr_e, va_s, va_e in CV_FOLDS:
        train = collect_race_records(races, tr_s, tr_e, weights, wr_blend)
        valid = collect_race_records(races, va_s, va_e, weights, wr_blend)

        # 学習区間で ROI 最大の閾値を選ぶ（学習N≥min_n のものから）。無ければ賭けない。
        best_t = None
        best_roi = -1.0
        for t in FUND_RANK_THRESHOLDS:
            m = strat_fn(train, t)
            if m["n"] >= min_n and m["roi"] > best_roi:
                best_roi = m["roi"]
                best_t = t

        if best_t is None:
            train_sel = {"n": 0, "hit": 0.0, "roi": 0.0}
            valid_sel = {"n": 0, "hit": 0.0, "roi": 0.0}
        else:
            train_sel = strat_fn(train, best_t)
            valid_sel = strat_fn(valid, best_t)

        valid_base = baseline_fav(valid)
        coverage = valid_sel["n"] / valid_base["n"] if valid_base["n"] else 0.0
        gap = train_sel["roi"] - valid_sel["roi"]

        folds.append(
            {
                "train": [tr_s, tr_e],
                "valid": [va_s, va_e],
                "selected_threshold": best_t,
                "train_sel": train_sel,
                "valid_sel": valid_sel,
                "valid_base": valid_base,
                "coverage": coverage,
                "gap": gap,
            }
        )
        logger.info(
            f"  学習{tr_s}-{tr_e}→検証{va_s}-{va_e}: 危険閾値={best_t or '(なし)'}"
        )
        logger.info(
            f"    学習ROI {train_sel['roi'] * 100:.1f}% / "
            f"検証ROI {valid_sel['roi'] * 100:.1f}% "
            f"(過学習ギャップ {gap * 100:+.1f}pt) / "
            f"被覆 {valid_sel['n']}/{valid_base['n']} ({coverage * 100:.0f}%) / "
            f"素の1番人気ROI {valid_base['roi'] * 100:.1f}%"
        )

    # 検証ROIを被覆レース数で加重平均＝過学習補正後の実力見積もり。
    cov_n = sum(f["valid_sel"]["n"] for f in folds)
    base_n = sum(f["valid_base"]["n"] for f in folds)
    valid_sel_overall = (
        sum(f["valid_sel"]["roi"] * f["valid_sel"]["n"] for f in folds) / cov_n
        if cov_n
        else 0.0
    )
    valid_base_overall = (
        sum(f["valid_base"]["roi"] * f["valid_base"]["n"] for f in folds) / base_n
        if base_n
        else 0.0
    )
    coverage_overall = cov_n / base_n if base_n else 0.0
    logger.info(
        f"  → 検証ROI加重平均（実力見積もり）: {valid_sel_overall * 100:.1f}% "
        f"/ 素の1番人気ROI: {valid_base_overall * 100:.1f}% / "
        f"被覆率: {coverage_overall * 100:.0f}%"
    )
    return {
        "folds": folds,
        "valid_sel_roi_overall": valid_sel_overall,
        "valid_base_roi_overall": valid_base_overall,
        "coverage_overall": coverage_overall,
    }


# ============================================================
# ログ出力 / メイン
# ============================================================


def _log_summary(label: str, m: dict) -> None:
    """fav_summary 1行をログ出力。"""
    logger.info(
        f"  {label:<10}N={m['n']:>6,}  勝率{m['hit'] * 100:>5.1f}%  "
        f"複勝{m['place'] * 100:>5.1f}%  ROI{m['roi'] * 100:>6.1f}%  "
        f"含意勝率{m['implied'] * 100:>5.1f}%"
    )


def run_diagnostic(races: dict, weights: dict, wr_blend: float) -> dict:
    """Part 1: IS全体の分離力診断（順位別・帯制御・3ブロック一貫）。"""
    logger.info(f"=== Part 1 診断: 危険な人気馬の分離力（IS {IS_START}-{IS_END}） ===")
    records = collect_race_records(races, IS_START, IS_END, weights, wr_blend)

    baseline = fav_summary(records)
    logger.info("1番人気ベースライン（run_backtest の fav と一致するはず）:")
    _log_summary("全1番人気", baseline)

    rank_buckets = by_rank_bucket(records)
    logger.info("ファンダ順位別の1番人気成績（順位が悪いほど負けるなら危険信号）:")
    for _, _, label in RANK_BUCKETS:
        _log_summary(label, rank_buckets[label])

    band_flag = by_band_and_flag(records)
    logger.info(
        f"オッズ帯制御（同帯内 agree=rank1 vs dangerous=rank>={DANGER_RANK}・市場超え増分の有無）:"
    )
    for band in BAND_LABELS:
        a = band_flag[band]["agree"]
        d = band_flag[band]["dangerous"]
        logger.info(
            f"  {band:<10}agree: N={a['n']:>5,} 勝率{a['hit'] * 100:>5.1f}% "
            f"ROI{a['roi'] * 100:>6.1f}%  | dangerous: N={d['n']:>5,} "
            f"勝率{d['hit'] * 100:>5.1f}% ROI{d['roi'] * 100:>6.1f}%"
        )

    consistency = block_consistency(races, weights, wr_blend)
    logger.info(
        f"3ブロック一貫ゲート（全ブロックで dangerous の勝率 < agree か）: "
        f"{'○ 一貫' if consistency['consistent'] else '× 不一致'}"
    )
    for b in consistency["blocks"]:
        logger.info(
            f"  {b['block'][0]}-{b['block'][1]}: agree勝率 "
            f"{b['agree']['hit'] * 100:.1f}% vs dangerous勝率 "
            f"{b['dangerous']['hit'] * 100:.1f}% "
            f"(差{b['win_gap'] * 100:+.1f}pt) {'○' if b['ok'] else '×'}"
        )

    return {
        "favorite_baseline": baseline,
        "by_fund_rank": rank_buckets,
        "by_band_flag": band_flag,
        "block_consistency": consistency,
    }


def run_smoke(races: dict, weights: dict, wr_blend: float) -> None:
    """スモーク: IS内の小窓で Part1 の中核だけ実行し、実行可否を確認（CV省略）。"""
    logger.info(f"=== スモーク（{SMOKE_START}-{SMOKE_END}・実行可否のみ） ===")
    records = collect_race_records(races, SMOKE_START, SMOKE_END, weights, wr_blend)
    _log_summary("全1番人気", fav_summary(records))
    for _, _, label in RANK_BUCKETS:
        sub = [r for r in records if label_in_bucket(r["fav_rank"], label)]
        _log_summary(label, fav_summary(sub))
    logger.info("スモーク完了（結果の良し悪しは判断材料にしない）。")


def label_in_bucket(rank: int, label: str) -> bool:
    """rank が RANK_BUCKETS の label に属するか。"""
    for lo, hi, lb in RANK_BUCKETS:
        if lb == label:
            return lo <= rank <= hi
    return False


def main() -> None:
    """診断とIS内CVを実行し、レポートを保存する（OOS封印）。"""
    ap = argparse.ArgumentParser(
        description="#B5 危険な人気馬（過剰人気）検出診断＋IS内CV（IS限定・OOS封印）"
    )
    ap.add_argument(
        "--wr-blend",
        type=float,
        default=DEFAULT_WR_BLEND,
        help="勝率/ROIブレンド比（既定0.6）",
    )
    ap.add_argument(
        "--min-n",
        type=int,
        default=30,
        help="CVで危険閾値を採用する学習区間ベット数の下限（既定30）",
    )
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="IS内小窓で実行可否のみ確認（CV省略・本番扱いしない）",
    )
    args = ap.parse_args()

    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    try:
        races = load_races(conn)
    finally:
        conn.close()

    if args.smoke:
        run_smoke(races, DEFAULT_WEIGHTS, args.wr_blend)
        return

    diagnostic = run_diagnostic(races, DEFAULT_WEIGHTS, args.wr_blend)

    logger.info(
        f"=== Part 2 IS内CV: 危険な人気馬の fade/avoid（IS {IS_START}-{IS_END}） ==="
    )
    cv = {
        "avoid": run_cv_strategy(
            races,
            DEFAULT_WEIGHTS,
            args.wr_blend,
            strat_avoid,
            args.min_n,
            "AVOID(避ける)",
        ),
        "fade": run_cv_strategy(
            races,
            DEFAULT_WEIGHTS,
            args.wr_blend,
            strat_fade,
            args.min_n,
            "FADE(逆らう)",
        ),
    }

    out = {
        "is_period": [IS_START, IS_END],
        "wr_blend": args.wr_blend,
        "min_n": args.min_n,
        "fund_rank_thresholds": FUND_RANK_THRESHOLDS,
        "danger_rank": DANGER_RANK,
        "odds_bands": BAND_LABELS,
        "diagnostic": diagnostic,
        "cross_validation": cv,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

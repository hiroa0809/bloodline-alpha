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
                   材料の有力候補。OFF馬場レースに限定して③を測る。

仮説と検証は M1 と同一: ①生AUC（予測力）と ③オッズ整合AUC（市場制御後の増分）を
analyze_subscore_signal の AUC 機構でそのまま評価する。

金庫ルール厳守:
  - 対象＝非新馬の平地戦・IS(1993-2013)限定・OOS(2014+)封印（SQLでも境界封印）。
  - point-in-time: 馬の過去走は対象レース日より厳密に前、標準は対象年より前の年のみ。
    日次トラック差は「過去走の当日（完了済み）」で作るため未来リークなし（その過去走は
    予測対象の将来レースより前に終わっている）。
  - しきい値（top_n / odds_ratio / auc_min / MIN_DAY_RUNS / 馬場バケツ）は事前登録。
    既存ライブスコア・M1 には触れない。

使い方:
    python backend/backtest/analyze_speed_signal_m2.py
    python backend/backtest/analyze_speed_signal_m2.py --smoke   # 小窓・実行可否のみ
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
from backtest.analyze_speed_signal import (  # noqa: E402
    _safe_chaku,
    parse_ato3f,
    parse_soha,
)
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
OUT_PATH = _BACKEND_DIR / "backtest" / "speed_signal_m2_report.json"

SCAN_START = 1986
SMOKE_SCAN_START, SMOKE_ANA_START, SMOKE_ANA_END = 2005, 2008, 2010
MIN_DAY_RUNS = 30  # 日次トラック差を適用する当日完走数の下限（未満は offset=0）

# 競走条件コード → クラス tier（高いほど上級）。701新馬は非新馬フィルタで除外。
_CLASS_TIER = {"703": 0, "005": 1, "010": 2, "016": 3, "999": 4}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ============================================================
# クラス tier / 馬場バケツ
# ============================================================


def race_class_tier(codes: tuple, grade_code: str | None) -> int | None:
    """競走条件コード群 → クラス tier。非'000'/非空コードの max。無→ grade有りでOP(4)。"""
    tiers = [
        _CLASS_TIER[(c or "").strip()]
        for c in codes
        if (c or "").strip() in _CLASS_TIER
    ]
    if tiers:
        return max(tiers)
    if grade_code and grade_code.strip() not in ("", "0"):
        return 4
    return None  # 不明クラス → クラス非依存標準へフォールバック


def going_bucket(
    surface: str | None, shiba: str | None, dirt: str | None
) -> str | None:
    """馬場状態 → 'off'(重/不良) / 'good'(良/稍重) / None(未設定)。事前登録バケツ。"""
    if not surface:
        return None
    code = ((shiba if surface == "turf" else dirt) or "").strip()
    if code in ("3", "4"):
        return "off"
    if code in ("1", "2"):
        return "good"
    return None


# ============================================================
# データロード
# ============================================================


def load_race_info(conn: sqlite3.Connection, scan_start: int, ana_end: int) -> dict:
    """jvd_race → レースPK → 条件情報（surface/class_tier/going_bucket 含む）。"""
    info: dict[tuple, dict] = {}
    cur = conn.execute(
        "SELECT kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, "
        "  race_bango, track_code, kyori, grade_code, "
        "  kyoso_joken_code_2sai, kyoso_joken_code_3sai, kyoso_joken_code_4sai, "
        "  kyoso_joken_code_5sai_ijo, shiba_baba_jotai_code, dirt_baba_jotai_code "
        "FROM jvd_race WHERE CAST(kaisai_nen AS INTEGER) BETWEEN ? AND ?",
        (scan_start, ana_end),
    )
    for (
        nen,
        tsukihi,
        keibajo,
        kai,
        nichime,
        rbango,
        track,
        kyori,
        grade,
        j2,
        j3,
        j4,
        j5,
        shiba,
        dirt,
    ) in cur:
        try:
            year = int(nen)
        except (ValueError, TypeError):
            continue
        surface = track_to_surface(track)  # 障害は None
        # 条件コードは固定長/空白付きで返り得るため strip 済み値を is_maiden/class_tier で共有。
        codes = tuple((c or "").strip() for c in (j2, j3, j4, j5))
        info[(nen, tsukihi, keibajo, kai, nichime, rbango)] = {
            "date": f"{nen}{tsukihi}",
            "year": year,
            "surface": surface,
            "kyori": (kyori or "").strip() or None,
            "is_maiden": "701" in codes[:2],
            "class_tier": race_class_tier(codes, grade),
            "going_bucket": going_bucket(surface, shiba, dirt),
        }
    return info


def load_runs(conn: sqlite3.Connection, race_info: dict, scan_start: int, ana_end: int):
    """jvd_race_uma を走査し平地戦の出走行を収集（year<=ana_end＝OOS封印）。"""
    runs: list[dict] = []
    cur = conn.execute(
        "SELECT kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, "
        "  race_bango, umaban, ketto_toroku_bango, kakutei_chakujun, tansho_odds, "
        "  ijo_kubun_code, soha_time, ato_3f_time FROM jvd_race_uma "
        "WHERE CAST(kaisai_nen AS INTEGER) BETWEEN ? AND ?",
        (scan_start, ana_end),
    )
    for (
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
    ) in cur:
        info = race_info.get((nen, tsukihi, keibajo, kai, nichime, rbango))
        if info is None or info["surface"] is None:
            continue
        year = info["year"]
        if not (scan_start <= year <= ana_end):
            continue
        if not (umaban and umaban.strip()):
            continue
        ketto_id = (ketto or "").strip()
        if not ketto_id:
            continue  # 空 ketto は別馬同士が同一集約キーに混ざるため除外
        ijo_code = (ijo or "").strip()
        try:
            odds_real = int(odds) / 10.0 if odds and odds.strip() else None
        except (ValueError, TypeError):
            odds_real = None
        chaku_i = _safe_chaku(chaku)
        valid_finish = chaku_i >= 1 and ijo_code in ("", "0")
        runs.append(
            {
                "date": info["date"],
                "year": year,
                "race_id": f"{nen}{tsukihi}{keibajo}{kai}{nichime}{rbango}",
                "umaban": umaban.strip(),
                "ketto": ketto_id,
                "chaku": chaku_i,
                "odds": odds_real,
                "is_maiden": info["is_maiden"],
                "key": (keibajo, info["surface"], info["kyori"]),
                "class_tier": info["class_tier"],
                "going_bucket": info["going_bucket"],
                "soha": parse_soha(soha) if valid_finish else None,
                "ato3f": parse_ato3f(ato3f) if valid_finish else None,
            }
        )
    return runs


# ============================================================
# 標準タイム（クラスキー）→ 補正後指数 → 馬ごと集約
# ============================================================


def _cumulative_median(by_key_year: dict) -> dict:
    """{key:{year:[vals]}} → {key:{year: その年“より前”の累積中央値}}（未来リークゼロ）。"""
    out: dict = {}
    for k, ym in by_key_year.items():
        acc: list[float] = []
        sd: dict[int, float] = {}
        for y in sorted(ym):
            if acc:
                sd[y] = statistics.median(acc)
            acc.extend(ym[y])
        out[k] = sd
    return out


def build_standards(runs: list[dict], field: str):
    """(plain標準, クラスキー標準) を返す。field='soha'|'ato3f'。"""
    plain: dict = {}
    cls: dict = {}
    for r in runs:
        t = r[field]
        if t is None:
            continue
        plain.setdefault(r["key"], {}).setdefault(r["year"], []).append(t)
        ct = r["class_tier"]
        if ct is not None:
            cls.setdefault((r["key"], ct), {}).setdefault(r["year"], []).append(t)
    return _cumulative_median(plain), _cumulative_median(cls)


def _fig0(r, field, std_plain, std_cls):
    """(補正前指数 fig0=クラス標準−自タイム, M1基準指数=plain標準−自タイム) を返す。"""
    t = r[field]
    if t is None:
        return None, None
    plain_base = std_plain.get(r["key"], {}).get(r["year"])
    base = None
    ct = r["class_tier"]
    if ct is not None:
        base = std_cls.get((r["key"], ct), {}).get(r["year"])
    if base is None:
        base = plain_base
    fig0 = (base - t) if base is not None else None
    base0 = (plain_base - t) if plain_base is not None else None
    return fig0, base0


def _apply_day_variant(runs, fig_attr, out_attr, min_day_runs):
    """(date,競馬場,芝/ダート) 別に fig0 の中央オフセットを差し引く＝馬場差（日次トラック差）。"""
    groups: dict = {}
    for r in runs:
        f = r.get(fig_attr)
        if f is not None:
            groups.setdefault((r["date"], r["key"][0], r["key"][1]), []).append(f)
    offsets = {
        gk: statistics.median(v) for gk, v in groups.items() if len(v) >= min_day_runs
    }
    for r in runs:
        f = r.get(fig_attr)
        if f is None:
            r[out_attr] = None
            continue
        off = offsets.get((r["date"], r["key"][0], r["key"][1]), 0.0)
        r[out_attr] = f - off


def assign_figures(runs: list[dict], min_day_runs: int) -> None:
    """各 run に M1基準指数(base_soha) と 補正後指数(cv_soha/cv_ato3f) を付与する。"""
    std_plain_s, std_cls_s = build_standards(runs, "soha")
    std_plain_a, std_cls_a = build_standards(runs, "ato3f")
    for r in runs:
        fig0_s, base0_s = _fig0(r, "soha", std_plain_s, std_cls_s)
        fig0_a, _ = _fig0(r, "ato3f", std_plain_a, std_cls_a)
        r["_fig0_soha"] = fig0_s
        r["_fig0_ato3f"] = fig0_a
        r["base_soha"] = base0_s  # M1基準（クラス・馬場差なし）
    _apply_day_variant(runs, "_fig0_soha", "cv_soha", min_day_runs)
    _apply_day_variant(runs, "_fig0_ato3f", "cv_ato3f", min_day_runs)


def assign_horse_features(runs: list[dict]) -> None:
    """date昇順で各 run に strictly-prior の過去走集約を付与（M1 と同方針）。

    補正後 soha: best/recent/avg、補正後 ato3f: best、M1基準 soha: best/recent、
    馬場適性 going_apt = mean(補正後soha|過去OFF走) − mean(同|過去GOOD走)（両方向・符号付き）。
    """
    runs.sort(key=lambda r: (r["date"], r["race_id"], r["umaban"]))
    agg: dict[str, dict] = {}
    for r in runs:
        st = agg.get(r["ketto"])
        if st is None or st["sc"] == 0:
            r["f_cv_best"] = r["f_cv_recent"] = r["f_cv_avg"] = np.nan
        else:
            r["f_cv_best"] = st["sb"]
            r["f_cv_recent"] = st["sl"]
            r["f_cv_avg"] = st["ss"] / st["sc"]
        r["f_cv_ato_best"] = (
            st["ab"] if (st is not None and not np.isnan(st["ab"])) else np.nan
        )
        if st is not None and st["bc"] > 0:
            r["f_base_best"] = st["bb"]
            r["f_base_recent"] = st["bl"]
        else:
            r["f_base_best"] = r["f_base_recent"] = np.nan
        if st is not None and st["off_c"] >= 1 and st["good_c"] >= 1:
            r["f_going_apt"] = st["off_s"] / st["off_c"] - st["good_s"] / st["good_c"]
        else:
            r["f_going_apt"] = np.nan
        # 自分の値で集約を更新（次走以降の過去走になる）
        if st is None:
            st = {
                "sb": np.nan,
                "ss": 0.0,
                "sc": 0,
                "sl": np.nan,
                "ab": np.nan,
                "bb": np.nan,
                "bl": np.nan,
                "bc": 0,
                "off_s": 0.0,
                "off_c": 0,
                "good_s": 0.0,
                "good_c": 0,
            }
            agg[r["ketto"]] = st
        cv = r["cv_soha"]
        if cv is not None:
            st["sb"] = cv if np.isnan(st["sb"]) else max(st["sb"], cv)
            st["ss"] += cv
            st["sc"] += 1
            st["sl"] = cv
            if r["going_bucket"] == "off":
                st["off_s"] += cv
                st["off_c"] += 1
            elif r["going_bucket"] == "good":
                st["good_s"] += cv
                st["good_c"] += 1
        cva = r["cv_ato3f"]
        if cva is not None:
            st["ab"] = cva if np.isnan(st["ab"]) else max(st["ab"], cva)
        bs = r["base_soha"]
        if bs is not None:
            st["bb"] = bs if np.isnan(st["bb"]) else max(st["bb"], bs)
            st["bl"] = bs
            st["bc"] += 1


# ============================================================
# 解析用 `a` 配列
# ============================================================

# (name, run属性, ラベル, 診断スコープ 'all'|'off')
FEATURES = [
    ("soha_best_base", "f_base_best", "走破best(M1基準)", "all"),
    ("soha_recent_base", "f_base_recent", "走破直近(M1基準)", "all"),
    ("soha_best", "f_cv_best", "走破best(補正)", "all"),
    ("soha_recent", "f_cv_recent", "走破直近(補正)", "all"),
    ("soha_avg", "f_cv_avg", "走破平均(補正)", "all"),
    ("ato3f_best", "f_cv_ato_best", "上り3Fbest(補正)", "all"),
    ("going_apt", "f_going_apt", "馬場適性(両方向)", "off"),
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
    for fname, fkey, _, _ in FEATURES:
        vals = np.array([float(r[fkey]) for r in targets], dtype=np.float64)
        a[fname] = vals
        a[f"{fname}_m"] = ~np.isnan(vals)
    if n == 0:
        a["race_start"] = np.array([0], dtype=np.int64)
        a["race_year"] = np.array([], dtype=np.int64)
        a["race_is_off"] = np.array([], dtype=bool)
        return a
    rid = np.array([r["race_id"] for r in targets], dtype=object)
    year = np.array([r["year"] for r in targets], dtype=np.int64)
    bucket = np.array([r["going_bucket"] or "" for r in targets], dtype=object)
    change = (year[1:] != year[:-1]) | (rid[1:] != rid[:-1])
    starts = np.concatenate(([0], np.nonzero(change)[0] + 1, [n])).astype(np.int64)
    a["race_start"] = starts
    a["race_year"] = year[starts[:-1]]
    a["race_is_off"] = np.array([bucket[s] == "off" for s in starts[:-1]], dtype=bool)
    n_off = int(a["race_is_off"].sum())
    logger.info(
        f"  対象（非新馬・平地）: {n:,} 頭 / {len(starts) - 1:,} レース"
        f"（うち OFF馬場 {n_off:,} レース）"
    )
    return a


def race_indices_off(a: dict, y0: int, y1: int) -> list[tuple[int, int]]:
    """年範囲 [y0,y1] かつ OFF馬場（重/不良）のレースだけの頭スライスを返す。"""
    ry = a["race_year"]
    starts = a["race_start"]
    off = a["race_is_off"]
    r0 = int(np.searchsorted(ry, y0, "left"))
    r1 = int(np.searchsorted(ry, y1, "right"))
    return [(int(starts[r]), int(starts[r + 1])) for r in range(r0, r1) if off[r]]


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

    results = []
    for fname, _, label, scope in FEATURES:
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
    speed_edge = [
        r
        for r in results
        if r["feature"] in ("soha_best", "soha_recent", "soha_avg")
        and r["verdict"] == "エッジ候補"
    ]
    apt = next((r for r in results if r["feature"] == "going_apt"), None)
    apt_edge = bool(apt and apt["verdict"] == "エッジ候補")
    gate_pass = len(speed_edge) > 0 or apt_edge
    logger.info(
        f"=== go/no-go: 補正スピード{'通過' if speed_edge else '不通過'} / "
        f"馬場適性{'通過' if apt_edge else '不通過'}"
        f"（適性 被覆={apt['coverage'] if apt else 0:,}頭・③整合ペア={apt['matched_pairs'] if apt else 0:,}） ==="
    )
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
        "market_reference_auc": market_pooled,
        "features": results,
        "gate_pass_speed": len(speed_edge) > 0,
        "gate_pass_aptitude": apt_edge,
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
            f"スモーク: クラスtier分布(非新馬)={dict(sorted(tiers.items(), key=lambda kv: (kv[0] is None, kv[0])))}"
        )
        logger.info("スモーク完了（結果の良し悪しは判断材料にしない）。")
        return
    OUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

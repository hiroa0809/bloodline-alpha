"""Direction A M2 スピード指数の特徴量構築層（データロード→補正後指数→馬ごと集約→配列化）。

`analyze_speed_signal_m2.py` から分離した構築ロジック。クラス補正・馬場差（日次トラック差）・
馬場適性（両方向・過去OFF走数しきい値変種）を as-of（point-in-time）で組み立てる。
診断（AUC評価・go/no-go・出力）は呼び出し側に置く。設計と金庫ルールは呼び出し側の docstring 参照。
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
import sys
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
from backtest.asof_helpers import track_to_surface  # noqa: E402

MIN_DAY_RUNS = 30  # 日次トラック差を適用する当日完走数の下限（未満は offset=0）

# 競走条件コード → クラス tier（高いほど上級）。701新馬は非新馬フィルタで除外。
_CLASS_TIER = {"703": 0, "005": 1, "010": 2, "016": 3, "999": 4}

# 馬場適性の過去OFF走数 感度しきい値（事前登録）。同じ going_apt 値を、過去OFF走が
# 各しきい値以上の馬だけに絞って診断＝サンプル不足由来のブレかを切り分ける。
GOING_FLOORS = (1, 2, 3)

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
        # 過去OFF走数（感度しきい値で絞る用）。strictly-prior の本数。
        r["going_apt_offc"] = st["off_c"] if st is not None else 0
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
    # 馬場適性: 同一の going_apt 値を過去OFF走数しきい値で絞った変種群を作る。
    gv = np.array([float(r["f_going_apt"]) for r in targets], dtype=np.float64)
    goffc = np.array([int(r["going_apt_offc"]) for r in targets], dtype=np.int64)
    a["_going_offc"] = goffc  # OFF走数分布レポート用
    for fl in GOING_FLOORS:
        name = f"going_apt_off{fl}"
        a[name] = gv
        a[f"{name}_m"] = (~np.isnan(gv)) & (goffc >= fl)
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

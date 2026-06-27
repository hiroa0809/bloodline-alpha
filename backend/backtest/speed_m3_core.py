"""Direction A M3a スピード指数の「馬ごとペース／トリップ補正」特徴量構築層。

M2（speed_m2_core）でクラス補正＋馬場差を入れた補正後指数 cv_soha は、走破直近の
③オッズ整合AUC が直近ブロックでも 0.524（ゲート通過・薄い増分）まで来た。M3a はこれを
さらに鋭くする精緻化として、馬ごとの「トリップ（展開の不利）」を独立特徴で診断する。

設計の核（なぜ馬ごとか）:
  ③オッズ整合AUC もエッジベットも「レース内比較」。レース単位の値（mae3f＝全馬共通の前半
  ペース）を引いてもレース内順位は動かない＝ペース補正は馬ごと（トリップ）でなければ信号に
  効かない。効くのは各馬のコーナー通過順位（直線手前の位置）。後方から・速い流れの中を
  好タイムで上がった馬はタイムを過小評価されがちで市場も見落としやすい。

特徴（フィット係数なし・馬場適性と同じ "フィルタ型"。係数を当てず母集団を絞るだけ）:
  - trip_*       : strictly-prior の「後方トリップ走に限った cv_soha の best/recent」。
                   後方＝直線手前位置が出走頭数の後半（back_frac >= BACK_CUT）。
  - trip_pace_*  : 上記をさらに「速ペース走（as-of標準 mae3f より速い前半）」に限定。
  サンプル感度: 後方走数 >=1/>=2/>=3（M2 GOING_FLOORS と同方式）で、ブレがサンプル不足
  由来かを切り分ける。

point-in-time / 金庫ルール:
  コーナー順位・mae3f は馬の過去走（対象日より厳密に前）由来。ペース標準は対象年“より前”の
  年のみ（cumulative median）＝未来リークなし。しきい値（BACK_CUT / severity 符号 / 後方
  走数しきい値）は事前登録。診断・ゲートは呼び出し側 analyze_speed_signal_m3 に置く。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from backtest.speed_m2_core import _cumulative_median  # noqa: E402

# 後方判定の事前登録しきい値。back_frac=(pos-1)/(field_size-1)∈[0,1]（0=先頭,1=最後方）。
BACK_CUT = 0.5  # 直線手前で出走頭数の後半に居た＝後方トリップ
# 後方トリップ過去走数の感度しきい値（事前登録・M2 GOING_FLOORS と同方式）。
TRIP_FLOORS = (1, 2, 3)


# ============================================================
# トリップ／ペースの素材計算
# ============================================================


def _prestretch_pos(corners: tuple) -> int | None:
    """直線手前の位置＝最後の有効コーナー順位。距離でコーナー数が違うため最終非Noneを採る。"""
    for c in reversed(corners):
        if c is not None:
            return c
    return None


def _back_frac(pos: int | None, field_size: int | None) -> float | None:
    """後方度 (pos-1)/(field_size-1) ∈[0,1]。pos>頭数の不整合や頭数<2 は None。"""
    if pos is None or field_size is None or field_size < 2:
        return None
    if pos > field_size:  # 順位>出走頭数＝データ不整合
        return None
    return (pos - 1) / (field_size - 1)


def build_pace_standards(runs: list[dict]):
    """(plain標準, クラスキー標準) の as-of mae3f 中央値。レース単位で1値だけ採る。"""
    seen: set = set()
    by_key: dict = {}
    by_cls: dict = {}
    for r in runs:
        rid = r["race_id"]
        if rid in seen:
            continue
        seen.add(rid)  # 同一レースの全馬は同じ mae3f＝レース単位で1回だけ採用
        m = r["mae3f"]
        if m is None:
            continue
        by_key.setdefault(r["key"], {}).setdefault(r["year"], []).append(m)
        ct = r["class_tier"]
        if ct is not None:
            by_cls.setdefault((r["key"], ct), {}).setdefault(r["year"], []).append(m)
    return _cumulative_median(by_key), _cumulative_median(by_cls)


def _severity(r: dict, std_plain: dict, std_cls: dict) -> float | None:
    """as-of標準 mae3f − 当該レース mae3f（正＝標準より速い前半＝速い流れ）。"""
    m = r["mae3f"]
    if m is None:
        return None
    base = None
    ct = r["class_tier"]
    if ct is not None:
        base = std_cls.get((r["key"], ct), {}).get(r["year"])
    if base is None:
        base = std_plain.get(r["key"], {}).get(r["year"])
    if base is None:
        return None
    return base - m


# ============================================================
# 馬ごと as-of 集約（strictly-prior）
# ============================================================


def assign_trip_features(runs: list[dict], back_cut: float = BACK_CUT) -> None:
    """各 run に strictly-prior の後方トリップ集約を付与する（M2 assign_horse_features と同方針）。

    付与: f_trip_best/f_trip_recent（後方走の cv_soha best/recent）＋ trip_c（後方走数）、
          f_trip_pace_best/f_trip_pace_recent（後方×速ペース走）＋ trip_pace_c。
    """
    std_plain, std_cls = build_pace_standards(runs)
    runs.sort(key=lambda r: (r["date"], r["race_id"], r["umaban"]))
    agg: dict[str, dict] = {}
    for r in runs:
        st = agg.get(r["ketto"])
        if st is not None and st["tc"] > 0:
            r["f_trip_best"] = st["tb"]
            r["f_trip_recent"] = st["tl"]
        else:
            r["f_trip_best"] = r["f_trip_recent"] = np.nan
        r["trip_c"] = st["tc"] if st is not None else 0
        if st is not None and st["pc"] > 0:
            r["f_trip_pace_best"] = st["pb"]
            r["f_trip_pace_recent"] = st["pl"]
        else:
            r["f_trip_pace_best"] = r["f_trip_pace_recent"] = np.nan
        r["trip_pace_c"] = st["pc"] if st is not None else 0
        # 自分の値で集約を更新（後方トリップ走のみ＝次走以降の過去走になる）
        cv = r["cv_soha"]
        bf = _back_frac(_prestretch_pos(r["corners"]), r["field_size"])
        if cv is None or bf is None or bf < back_cut:
            continue
        if st is None:
            st = {
                "tb": np.nan,
                "tl": np.nan,
                "tc": 0,
                "pb": np.nan,
                "pl": np.nan,
                "pc": 0,
            }
            agg[r["ketto"]] = st
        st["tb"] = cv if np.isnan(st["tb"]) else max(st["tb"], cv)
        st["tl"] = cv
        st["tc"] += 1
        sev = _severity(r, std_plain, std_cls)
        if sev is not None and sev > 0:
            st["pb"] = cv if np.isnan(st["pb"]) else max(st["pb"], cv)
            st["pl"] = cv
            st["pc"] += 1


# ============================================================
# 解析用 `a` 配列（M2 build_arrays の target と厳密一致させて追記）
# ============================================================


def build_trip_arrays(a: dict, runs: list[dict], ana_end: int) -> list[tuple[str, str]]:
    """M2 の `a`（build_arrays 済み）にトリップ特徴の配列＋マスクを追記し、診断 spec を返す。

    target 選択・並びは build_arrays と厳密一致（非新馬・平地・year<=ana_end を
    (year, race_id, umaban) 昇順）。整合は assert で担保。
    """
    targets = [r for r in runs if not r["is_maiden"] and r["year"] <= ana_end]
    targets.sort(key=lambda r: (r["year"], r["race_id"], r["umaban"]))
    n = len(targets)
    assert n == len(a["chaku"]), (
        "M3 targets が M2 配列と整合しません（並び/フィルタ不一致）"
    )

    def _arr(attr: str) -> np.ndarray:
        return np.array([float(r[attr]) for r in targets], dtype=np.float64)

    tb = _arr("f_trip_best")
    tr = _arr("f_trip_recent")
    tpb = _arr("f_trip_pace_best")
    tpr = _arr("f_trip_pace_recent")
    tc = np.array([int(r["trip_c"]) for r in targets], dtype=np.int64)
    tpc = np.array([int(r["trip_pace_c"]) for r in targets], dtype=np.int64)

    specs: list[tuple[str, str]] = []
    # 主特徴: 後方トリップ走の best/recent を 後方走数しきい値で絞る感度変種。
    for base, vals, label in (
        ("trip_best", tb, "後方走破best"),
        ("trip_recent", tr, "後方走破直近"),
    ):
        for fl in TRIP_FLOORS:
            name = f"{base}_f{fl}"
            a[name] = vals
            a[f"{name}_m"] = (~np.isnan(vals)) & (tc >= fl)
            specs.append((name, f"{label}≥{fl}後方走"))
    # 副特徴: 後方×速ペース（mae3f 有り subset）。floor>=1 のみ（既に sparse）。
    for base, vals, label in (
        ("trip_pace_best", tpb, "後方×速ペースbest"),
        ("trip_pace_recent", tpr, "後方×速ペース直近"),
    ):
        a[base] = vals
        a[f"{base}_m"] = (~np.isnan(vals)) & (tpc >= 1)
        specs.append((base, label))

    # 分布レポート用（サンプル不足の切り分け）。
    a["_trip_c"] = tc
    a["_trip_pace_c"] = tpc
    a["_mae3f_have"] = np.array([r["mae3f"] is not None for r in targets], dtype=bool)
    return specs

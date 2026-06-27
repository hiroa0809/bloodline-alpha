"""Direction A M3b スピード指数の「上がり（末脚）速度＋大まくり（位置上昇）」特徴量構築層。

M2 はゲート通過したが薄い増分。M3a（馬ごとトリップ＝後方走の走破タイム）は③を厚くできず不通過。
ユーザー要件＝「スタートで出遅れても最後の直線で大まくりする馬のスピードが反映されるか」。

なぜ M3a では足りないか:
  我々の指数は2成分。走破タイム(soha)は出遅れ＋大外ロスで時計が膨らみ大まくり馬を不当に減点する
  （M3a はこの soha を使った）。上がり3F(ato_3f)は最後の600m＝スタート無関係で末脚をそのまま捉える。
  M3b は速度の軸を soha→上がり(末脚)に移し、さらに「最後の位置上昇＝大まくり」を独立特徴にする。

特徴（フィット係数なし・strictly-prior 集約）:
  - close_*    : 補正後上がり cv_ato3f の best/recent/avg（末脚速度＝出遅れ減点なし）。
  - makuri_*   : 大まくり走に限った cv_ato3f の best/recent。大まくり走＝直線手前で後方
                 (back_frac>=BACK_CUT)かつ着順まで positions_gained>=GAIN_MIN だけ前進した走り。
                 後方/大まくり走数 >=1/>=2/>=3 感度（M3a TRIP_FLOORS 同方式）。
  - slowpace_* : slow/neutral ペース走(severity<=0)に限った cv_ato3f best/recent（補助）。
                 mae3f は2000年より前が空のため 2000+ subset。診断側で 2000-2013 別ゲートにする。

point-in-time / 金庫: cv_ato3f・大まくり・ペースは馬の過去走（対象日より厳密に前）由来、ペース標準は
  対象年より前のみ。しきい値（BACK_CUT/GAIN_MIN/severity符号/TRIP_FLOORS）は事前登録。診断・ゲートは
  呼び出し側 analyze_speed_signal_m3b に置く。speed_m2_core/speed_m3_core は編集しない（再利用のみ）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from backtest.speed_m3_core import (  # noqa: E402
    BACK_CUT,
    TRIP_FLOORS,
    _back_frac,
    _prestretch_pos,
    _severity,
    build_pace_standards,
)

# 大まくり判定の事前登録しきい値。直線手前位置→着順で前進した数の下限。
GAIN_MIN = 3


# ============================================================
# 馬ごと as-of 集約（strictly-prior）
# ============================================================


def _is_makuri(r: dict) -> bool:
    """この過去走が大まくり（直線手前で後方→着順まで GAIN_MIN 以上前進）か。"""
    pos = _prestretch_pos(r["corners"])
    bf = _back_frac(pos, r["field_size"])
    chaku = r["chaku"]
    if bf is None or bf < BACK_CUT or chaku < 1 or pos is None:
        return False
    return (pos - chaku) >= GAIN_MIN


def assign_close_features(runs: list[dict], back_cut: float = BACK_CUT) -> None:
    """各 run に strictly-prior の上がり(末脚)集約を付与する（M2 assign_horse_features と同方針）。

    付与: f_close_best/recent/avg（補正後上がり cv_ato3f）、f_makuri_best/recent＋makuri_c、
          f_slowpace_best/recent＋slowpace_c（slow/neutralペース走・2000+ subset）。
    """
    std_plain, std_cls = build_pace_standards(runs)
    runs.sort(key=lambda r: (r["date"], r["race_id"], r["umaban"]))
    agg: dict[str, dict] = {}
    for r in runs:
        st = agg.get(r["ketto"])
        if st is not None and st["cc"] > 0:
            r["f_close_best"] = st["cb"]
            r["f_close_recent"] = st["cl"]
            r["f_close_avg"] = st["cs"] / st["cc"]
        else:
            r["f_close_best"] = r["f_close_recent"] = r["f_close_avg"] = np.nan
        if st is not None and st["mc"] > 0:
            r["f_makuri_best"] = st["mb"]
            r["f_makuri_recent"] = st["ml"]
        else:
            r["f_makuri_best"] = r["f_makuri_recent"] = np.nan
        r["makuri_c"] = st["mc"] if st is not None else 0
        if st is not None and st["pc"] > 0:
            r["f_slowpace_best"] = st["pb"]
            r["f_slowpace_recent"] = st["pl"]
        else:
            r["f_slowpace_best"] = r["f_slowpace_recent"] = np.nan
        r["slowpace_c"] = st["pc"] if st is not None else 0
        # 自分の値で集約を更新（補正後上がりがある走のみ＝次走以降の過去走になる）
        cv = r["cv_ato3f"]
        if cv is None:
            continue
        if st is None:
            st = {
                "cb": np.nan,
                "cl": np.nan,
                "cs": 0.0,
                "cc": 0,
                "mb": np.nan,
                "ml": np.nan,
                "mc": 0,
                "pb": np.nan,
                "pl": np.nan,
                "pc": 0,
            }
            agg[r["ketto"]] = st
        st["cb"] = cv if np.isnan(st["cb"]) else max(st["cb"], cv)
        st["cl"] = cv
        st["cs"] += cv
        st["cc"] += 1
        if _is_makuri(r):
            st["mb"] = cv if np.isnan(st["mb"]) else max(st["mb"], cv)
            st["ml"] = cv
            st["mc"] += 1
        sev = _severity(r, std_plain, std_cls)
        if sev is not None and sev <= 0:  # 標準と同等〜遅い前半＝展開が向かなかった
            st["pb"] = cv if np.isnan(st["pb"]) else max(st["pb"], cv)
            st["pl"] = cv
            st["pc"] += 1


# ============================================================
# 解析用 `a` 配列（M2 build_arrays の target と厳密一致させて追記）
# ============================================================


def build_close_arrays(a: dict, runs: list[dict], ana_end: int) -> dict:
    """M2 の `a`（build_arrays 済み）に末脚／大まくり特徴を追記し、診断 spec 群を返す。

    返り値 {"full": [(name,label),...], "sp": [(name,label),...]}（fullは1993-2013、spは2000-13補助）。
    target 選択・並びは build_arrays と厳密一致。整合は件数＋行キーで検証（assert は -O で消えるため raise）。
    """
    targets = [r for r in runs if not r["is_maiden"] and r["year"] <= ana_end]
    targets.sort(key=lambda r: (r["year"], r["race_id"], r["umaban"]))
    n = len(targets)
    if n != len(a["chaku"]):
        raise ValueError(
            f"M3b targets が M2 配列と整合しません（件数不一致 {n} != {len(a['chaku'])}）"
        )
    m3b_rid = np.array([r["race_id"] for r in targets], dtype=object)
    m3b_uma = np.array([r["umaban"] for r in targets], dtype=object)
    if not (
        np.array_equal(m3b_rid, a["_row_race_id"])
        and np.array_equal(m3b_uma, a["_row_umaban"])
    ):
        raise ValueError(
            "M3b targets が M2 配列と整合しません（行キー race_id/umaban 不一致）"
        )

    def _arr(attr: str) -> np.ndarray:
        return np.array([float(r[attr]) for r in targets], dtype=np.float64)

    full: list[tuple[str, str]] = []
    # 末脚速度（出遅れ減点なし・全頭で母集団が広い）。
    for name, attr, label in (
        ("close_best", "f_close_best", "上がりbest"),
        ("close_recent", "f_close_recent", "上がり直近"),
        ("close_avg", "f_close_avg", "上がり平均"),
    ):
        vals = _arr(attr)
        a[name] = vals
        a[f"{name}_m"] = ~np.isnan(vals)
        full.append((name, label))
    # 大まくり走に限った上がり。大まくり過去走数しきい値で絞る感度変種。
    mb = _arr("f_makuri_best")
    mr = _arr("f_makuri_recent")
    mc = np.array([int(r["makuri_c"]) for r in targets], dtype=np.int64)
    a["_makuri_c"] = mc
    for base, vals, label in (
        ("makuri_best", mb, "大まくりbest"),
        ("makuri_recent", mr, "大まくり直近"),
    ):
        for fl in TRIP_FLOORS:
            name = f"{base}_f{fl}"
            a[name] = vals
            a[f"{name}_m"] = (~np.isnan(vals)) & (mc >= fl)
            full.append((name, f"{label}≥{fl}大まくり走"))

    # 補助: slow/neutralペース走に限った上がり（mae3f 2000+ subset・診断側で2000-13ゲート）。
    sp: list[tuple[str, str]] = []
    pc = np.array([int(r["slowpace_c"]) for r in targets], dtype=np.int64)
    a["_slowpace_c"] = pc
    a["_mae3f_have"] = np.array([r["mae3f"] is not None for r in targets], dtype=bool)
    for name, attr, label in (
        ("slowpace_best", "f_slowpace_best", "緩ペース上がりbest"),
        ("slowpace_recent", "f_slowpace_recent", "緩ペース上がり直近"),
    ):
        vals = _arr(attr)
        a[name] = vals
        a[f"{name}_m"] = (~np.isnan(vals)) & (pc >= 1)
        sp.append((name, label))

    return {"full": full, "sp": sp}

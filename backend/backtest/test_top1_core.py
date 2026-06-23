"""top1_core（numpy ベクトル化）のゴールデンテスト。

run_backtest.compute_score（pure-Python・正解）と top1_core.score_all（numpy）が
全頭で一致し、Top-1 一致率もランダム重みで pure 実装（optimize_top1.top1_match_rate）と
一致することを検証する。計測装置の再実装はバグに気づきにくいため、本番前に必須。

    python backend/backtest/test_top1_core.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np

_BD = Path(__file__).resolve().parent.parent
if str(_BD) not in sys.path:
    sys.path.insert(0, str(_BD))
from backtest import top1_core as core  # noqa: E402
from backtest.optimize_top1 import (  # noqa: E402
    IS_END,
    IS_START,
    SUBS,
    top1_match_rate as pure_match,
)
from backtest.run_backtest import compute_score, load_races  # noqa: E402

DB = _BD / "bloodline.db"
SCORE_TOL = 1e-9


def main() -> None:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    races = load_races(conn)
    arrays = core.load_arrays(conn)
    conn.close()

    # pure 側の (race_id, umaban) → row（スコア再計算用）
    pure_rows = {
        (r["race_id"], str(r["umaban"])): r
        for runners in races.values()
        for r in runners
    }
    keys = [
        (arrays["race_id"][i], str(arrays["umaban"][i])) for i in range(arrays["N"])
    ]
    assert all(k in pure_rows for k in keys), "numpy 側のキーが pure に存在しない"

    rng = np.random.default_rng(0)
    max_score_diff = 0.0
    max_match_diff = 0.0
    for t in range(5):
        w = {s: float(rng.random()) for s in SUBS}
        blend = float(rng.random())

        sc = core.score_all(arrays, w, blend)
        pure_sc = np.array(
            [compute_score(pure_rows[k], w, blend) for k in keys], dtype=np.float64
        )
        score_diff = float(np.max(np.abs(sc - pure_sc)))
        max_score_diff = max(max_score_diff, score_diff)

        pm, pn = pure_match(races, IS_START, IS_END, w, blend)
        vm, vn = core.top1_match_rate(arrays, sc, IS_START, IS_END)
        match_diff = abs(pm - vm)
        max_match_diff = max(max_match_diff, match_diff)
        print(
            f"trial{t}: score_diff={score_diff:.2e} / "
            f"pure_match={pm:.6f}(N={pn}) vec_match={vm:.6f}(N={vn}) "
            f"diff={match_diff:.2e}"
        )
        assert pn == vn, f"分母レース数が不一致: pure={pn} vec={vn}"

    print(f"MAX score diff = {max_score_diff:.2e} (tol {SCORE_TOL:.0e})")
    print(f"MAX match-rate diff = {max_match_diff:.2e}")
    assert max_score_diff < SCORE_TOL, "スコアが pure と不一致！numpy 実装にバグ"
    assert max_match_diff < 1e-9, "一致率が pure と不一致！"
    print("✅ GOLDEN OK: numpy 版は pure-Python と一致")


if __name__ == "__main__":
    main()

"""top1_core（numpy ベクトル化）のゴールデンテスト。

run_backtest.compute_score（pure-Python・正解）と top1_core.score_all（numpy）が
全頭で一致し、Top-1 一致率もランダム重みで pure 実装（optimize_top1.top1_match_rate）と
一致することを検証する。計測装置の再実装はバグに気づきにくいため、本番前に必須。

    python backend/backtest/test_top1_core.py
"""

from __future__ import annotations

import math
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
    top1_loglik as pure_loglik,
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
    max_loglik_diff = 0.0
    for t in range(5):
        w = {s: float(rng.random()) for s in SUBS}
        blend = float(rng.random())

        sc = core.score_all(arrays, w, blend)
        pure_sc = np.array(
            [compute_score(pure_rows[k], w, blend) for k in keys], dtype=np.float64
        )
        abs_diff = np.abs(sc - pure_sc)
        bad = np.flatnonzero(~np.isfinite(abs_diff))
        assert bad.size == 0, f"スコア差分に NaN/inf: key={keys[int(bad[0])]}"
        score_diff = float(np.max(abs_diff))
        max_score_diff = max(max_score_diff, score_diff)

        pm, pn = pure_match(races, IS_START, IS_END, w, blend)
        vm, vn = core.top1_match_rate(arrays, sc, IS_START, IS_END)
        match_diff = abs(pm - vm)
        max_match_diff = max(max_match_diff, match_diff)
        assert pn == vn, f"分母レース数が不一致: pure={pn} vec={vn}"

        # 道B（対数尤度）も pure と一致するか（log-domain 化後の整合を担保）
        beta = 1.0 + 9.0 * float(rng.random())
        pl = pure_loglik(races, IS_START, IS_END, w, blend, beta)
        vl = core.top1_loglik(arrays, sc, IS_START, IS_END, beta)
        assert math.isfinite(pl) and math.isfinite(vl), (
            f"対数尤度が非有限: pl={pl} vl={vl}"
        )
        loglik_diff = abs(pl - vl)
        max_loglik_diff = max(max_loglik_diff, loglik_diff)
        print(
            f"trial{t}: score_diff={score_diff:.2e} / "
            f"match: pure={pm:.6f}(N={pn}) vec={vm:.6f}(N={vn}) diff={match_diff:.2e} / "
            f"loglik(β={beta:.2f}): pure={pl:.6f} vec={vl:.6f} diff={loglik_diff:.2e}"
        )

    print(f"MAX score diff = {max_score_diff:.2e} (tol {SCORE_TOL:.0e})")
    print(f"MAX match-rate diff = {max_match_diff:.2e}")
    print(f"MAX loglik diff = {max_loglik_diff:.2e}")
    assert max_score_diff < SCORE_TOL, "スコアが pure と不一致！numpy 実装にバグ"
    assert max_match_diff < 1e-9, "一致率が pure と不一致！"
    assert max_loglik_diff < 1e-9, "対数尤度が pure と不一致！"
    print("✅ GOLDEN OK: numpy 版は pure-Python と一致（スコア/一致率/対数尤度）")


if __name__ == "__main__":
    main()

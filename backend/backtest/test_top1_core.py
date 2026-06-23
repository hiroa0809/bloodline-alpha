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


def _synthetic(races_data: list) -> tuple[dict, np.ndarray]:
    """(scores, won) のレース列から最小 arrays と scores 配列を作る（決定論的 edge テスト用）。"""
    scores: list = []
    won: list = []
    starts = [0]
    for sc_list, won_list in races_data:
        scores += sc_list
        won += won_list
        starts.append(len(scores))
    a = {
        "won": np.array(won, dtype=np.int64),
        "ninki": np.zeros(len(scores), dtype=np.int64),
        "race_start": np.array(starts, dtype=np.int64),
        "race_year": np.array([2000] * (len(starts) - 1), dtype=np.int64),
        "N": len(scores),
        "R": len(starts) - 1,
    }
    return a, np.array(scores, dtype=np.float64)


def check_edge_cases() -> None:
    """同点1位・勝者不在・極端βの分岐を決定論的に踏む（実DB＋乱数では確実に踏めない）。"""
    # 同点1位(2頭)→不一致 / 明確1位=1着→一致 ⇒ rate 0.5
    a, sc = _synthetic([([1.0, 1.0, 0.5], [1, 0, 0]), ([0.9, 0.2, 0.1], [1, 0, 0])])
    rate, n = core.top1_match_rate(a, sc, 2000, 2000)
    assert n == 2 and abs(rate - 0.5) < 1e-12, f"tie分岐: rate={rate} n={n}"
    # 勝者不在レースは loglik 分母から除外（勝者ありレースのみ計上）
    a2, sc2 = _synthetic([([0.5, 0.3], [0, 0]), ([0.8, 0.1], [1, 0])])
    assert math.isfinite(core.top1_loglik(a2, sc2, 2000, 2000, 1.0))
    # 勝者が最高スコアでない＋極端β: log-domain なら finite（旧 ex/denom は -inf に潰れる）
    a3, sc3 = _synthetic([([0.1, 0.8], [1, 0])])
    ll = core.top1_loglik(a3, sc3, 2000, 2000, 1e3)
    assert math.isfinite(ll), f"underflow で -inf 潰れ: ll={ll}"
    print(
        f"✅ EDGE OK: tie/no-winner/large-β 分岐を確認（underflowケース ll={ll:.3f}）"
    )


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
    check_edge_cases()


if __name__ == "__main__":
    main()

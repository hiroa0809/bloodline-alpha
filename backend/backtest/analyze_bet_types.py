"""#B5「数撃ちゃ当たる」券種別ボックス診断（順序なし6券種）。

これまでの #B5（Stage A〜D・サブ項目信号診断）で「ファンダ単独では新馬戦の“単勝”市場に
勝てる増分エッジが無い（生の予測力は実在するが単勝市場に完全に織り込み済み）」と確証した。
未検証なのは「その予測力が単勝以外のプール（複勝・馬連・ワイド・3連複・枠連）でも同じく
織り込まれているか」。パリミュチュエルでは穴券種ほどプールが薄く非効率になりやすいため、
別市場では価格が歪んでいる可能性が残る。本スクリプトはユーザー方針「下手な鉄砲も数撃ちゃ
当たる」＝目的を単勝Top-1から各種ボックス/組合せ買いに変え、+EV のプールを横断探索する。

金庫ルール（CLAUDE.md「バックテスト方法論」）を厳守:
  - 探索は IS（1993-2013）限定。OOS-1〜3 は本スクリプトで一切評価しない（封印）。
  - 券種・ボックスサイズ N は事前登録の固定グリッド（結果を見て後から動かさない）。
  - 配点は固定（再最適化しない）。本命＝DEFAULT（IS非適合＝CVが正直）。Stage D Top-1 重みは
    「IS適合済み＝楽観・着内ランクの天井値」と明示した参考感度行として Part1 のみ併記。

券種ごとの「当たり」判定は払戻表（jvd_haraimodoshi）の当選エントリを真実とする（着順から自前で
勝者を復元しない）。同着・降着・非発売（馬連91年〜/ワイド99年〜/3連複02年〜＝該当年JSONが
null）を払戻表がそのまま反映するため、JRAルールのハードコードを避けバグを構造的に減らす。

2部構成:
  Part 1 IS全体診断 — 各(券種, N)セルの 賭け可能N/的中率/ROI/被覆 を表出力（DEFAULT＋Stage D感度）。
  Part 2 IS内CV — 券種・Nは事前登録ゆえ各セルの検証ROIを3fold横断でプール（過学習補正後の実力）。
    補助で「学習ROI最良セルを選び検証で採点」した選択回帰も併記（最良券種が未見でどれだけ崩れるか）。

使い方:
    python backend/backtest/analyze_bet_types.py
    python backend/backtest/analyze_bet_types.py --min-n 50
    python backend/backtest/analyze_bet_types.py --smoke   # 狭い年範囲で実行可否のみ確認
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from backtest.run_backtest import (  # noqa: E402
    DEFAULT_WEIGHTS,
    DEFAULT_WR_BLEND,
    _safe_int,
    compute_score,
    evaluate,
    load_races,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "bet_type_report.json"
TOP1_WEIGHTS_PATH = _BACKEND_DIR / "backtest" / "top1_weights.json"

# IS（学習区間。CLAUDE.md「バックテスト方法論」）。OOS は封印。
IS_START, IS_END = 1993, 2013

# IS内ウォークフォワードCV分割（学習開始, 学習終了, 検証開始, 検証終了）。
# robustness.py / analyze_odds_bands.py の CV_FOLDS と同一値。
CV_FOLDS = [
    (1993, 2005, 2006, 2008),
    (1993, 2008, 2009, 2011),
    (1993, 2011, 2012, 2013),
]

# 事前登録グリッド: (券種キー=払戻列名, 表示名, 種別, N候補)。
#   種別 place=複勝(各馬), pair=馬連/ワイド(C(N,2)), triple=3連複(C(N,3)), bracket=枠連(枠ペア)。
# 単勝は基準ベースラインとして別扱い（キャッシュの単勝オッズで run_backtest を再現）。
GRID = [
    ("fukusho", "複勝", "place", [1, 2, 3, 4, 5]),
    ("umaren", "馬連", "pair", [2, 3, 4, 5]),
    ("wide", "ワイド", "pair", [2, 3, 4, 5]),
    ("sanrenpuku", "3連複", "triple", [3, 4, 5]),
    ("wakuren", "枠連", "bracket", [2, 3, 4, 5]),
]
MAX_N = 5  # 上位何頭まで選ぶか（グリッド最大）

# 払戻 kumiban のパース位置（開始, 長さ）。単勝/複勝は umaban を単独で扱う。
_KUMI_POS = {
    "wakuren": [(0, 1), (1, 1)],  # 枠連: 1桁枠×2（"12"→{1,2}, "11"→{1}）
    "umaren": [(0, 2), (2, 2)],  # 馬連: 2桁馬番×2
    "wide": [(0, 2), (2, 2)],  # ワイド: 2桁馬番×2
    "sanrenpuku": [(0, 2), (2, 2), (4, 2)],  # 3連複: 2桁馬番×3
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ============================================================
# 払戻・枠番のロード（race_id でキー）
# ============================================================
def _race_id(nen, tsukihi, keibajo, kai, nichime, rbango) -> str:
    """precompute_subscores と同一の race_id 連結。払戻PK・race_uma から復元する。"""
    return f"{nen}{tsukihi}{keibajo}{kai}{nichime}{rbango}"


def _loads(s: str | None) -> list:
    """払戻列をリスト化。DB は Python repr（シングルクォート）格納だが将来の json.dumps
    再インポートにも備え、json を試し失敗時 ast.literal_eval にフォールバックする。
    None / "null" / 非list は空リストに正規化する（非発売・異常値で落ちないように）。"""
    if not s:
        return []
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        try:
            parsed = ast.literal_eval(s)
        except (SyntaxError, ValueError, TypeError):
            return []
    return parsed if isinstance(parsed, list) else []


def _parse_single(js: str) -> list[tuple[frozenset, float]]:
    """単勝/複勝 JSON [{umaban, haraimodoshi_kin}, ...] → [(frozenset({馬番}), 倍率)]。"""
    out = []
    for e in _loads(js):
        try:
            ban = int(e["umaban"])
            kin = int(e["haraimodoshi_kin"])
        except (KeyError, ValueError, TypeError):
            continue
        if ban > 0 and kin > 0:
            out.append((frozenset({ban}), kin / 100.0))
    return out


def _parse_kumi(
    js: str, positions: list[tuple[int, int]]
) -> list[tuple[frozenset, float]]:
    """連系 JSON [{kumiban, haraimodoshi_kin}, ...] → [(frozenset({番...}), 倍率)]。"""
    out = []
    for e in _loads(js):
        try:
            kumi = e["kumiban"]
            kin = int(e["haraimodoshi_kin"])
        except (KeyError, ValueError, TypeError):
            continue
        if kin <= 0:
            continue
        try:
            nums = frozenset(int(kumi[s : s + ln]) for s, ln in positions)
        except (ValueError, TypeError, IndexError):
            continue
        if all(n > 0 for n in nums):
            out.append((nums, kin / 100.0))
    return out


def load_payoffs(conn: sqlite3.Connection, race_set: set[str]) -> dict[str, dict]:
    """IS の対象 race の払戻を {race_id: {券種: [(frozenset, 倍率), ...]}} で返す。

    券種JSONが null（非発売）の race はそのキーを持たない＝賭け不能として後段で除外される。
    """
    cur = conn.execute(
        "SELECT kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, "
        "  race_bango, fukusho, wakuren, umaren, wide, sanrenpuku "
        "FROM jvd_haraimodoshi WHERE kaisai_nen BETWEEN ? AND ?",
        (str(IS_START), str(IS_END)),
    )
    payoffs: dict[str, dict] = {}
    for nen, tsukihi, kj, kai, nichi, rb, fuku, waku, uren, wd, sanp in cur:
        rid = _race_id(nen, tsukihi, kj, kai, nichi, rb)
        if rid not in race_set:
            continue
        d: dict = {}
        if fuku:
            ent = _parse_single(fuku)
            if ent:
                d["fukusho"] = ent
        for col, js in (
            ("wakuren", waku),
            ("umaren", uren),
            ("wide", wd),
            ("sanrenpuku", sanp),
        ):
            if js:
                ent = _parse_kumi(js, _KUMI_POS[col])
                if ent:
                    d[col] = ent
        if d:
            payoffs[rid] = d
    return payoffs


def load_wakuban(
    conn: sqlite3.Connection, race_set: set[str]
) -> dict[str, dict[int, int]]:
    """IS の対象 race の枠番を {race_id: {馬番: 枠番}} で返す（枠連のbox構築用）。"""
    cur = conn.execute(
        "SELECT kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, "
        "  race_bango, umaban, wakuban "
        "FROM jvd_race_uma WHERE kaisai_nen BETWEEN ? AND ?",
        (str(IS_START), str(IS_END)),
    )
    wk: dict[str, dict[int, int]] = {}
    for nen, tsukihi, kj, kai, nichi, rb, umaban, wakuban in cur:
        rid = _race_id(nen, tsukihi, kj, kai, nichi, rb)
        if rid not in race_set:
            continue
        u, b = _safe_int(umaban), _safe_int(wakuban)
        if u > 0 and b > 0:
            wk.setdefault(rid, {})[u] = b
    return wk


# ============================================================
# スコアリング（固定配点で上位N頭を選ぶ）
# ============================================================
def build_picks(races: dict, weights: dict, wr_blend: float) -> dict[str, dict]:
    """IS各レースで上位 MAX_N 頭（馬番）を選ぶ。{race_id: {year, order, top1_odds, top1_won}}。

    並びは run_backtest.evaluate と同一（スコア降順・同点は馬番昇順）。order は馬番(int)列。
    """
    picks: dict[str, dict] = {}
    for rid, runners in races.items():
        y = runners[0]["as_of_year"]
        if not (IS_START <= y <= IS_END):
            continue
        scored = [
            (
                compute_score(r, weights, wr_blend),
                _safe_int(r["umaban"]),
                r["tansho_odds"],
                r["won"],
            )
            for r in runners
        ]
        scored.sort(key=lambda t: (-t[0], t[1]))  # スコア降順・馬番昇順
        order = [t[1] for t in scored[:MAX_N]]
        top = scored[0]
        picks[rid] = {
            "year": y,
            "order": order,
            "top1_odds": top[2],
            "top1_won": 1 if top[3] == 1 else 0,
        }
    return picks


# ============================================================
# 1レース×セルの賭け（払戻表駆動・100円単位）
# ============================================================
def _eval_race(kind: str, order: list[int], n: int, wins: list, waku: dict | None):
    """1レース1セルの (賭け点数, 払戻倍率合計) を返す。賭け不能なら None。

    100円単位: 賭け点数=組合せ数、払戻倍率合計=Σ(配当/100)。ROI は上位で Σ払戻/Σ賭け。
    """
    box = order[:n]
    if kind == "place":  # 複勝: 各馬単独
        bet = {frozenset({u}) for u in box}
    elif kind == "pair":  # 馬連/ワイド: C(N,2)
        bet = {frozenset(c) for c in combinations(box, 2)}
    elif kind == "triple":  # 3連複: C(N,3)
        bet = {frozenset(c) for c in combinations(box, 3)}
    elif kind == "bracket":  # 枠連: 上位の枠から枠ペア（同枠は2頭以上で成立）
        if waku is None or any(u not in waku for u in box):
            return None  # 上位N頭の枠番が揃わない＝意図したボックスを作れず賭け不能
        brs = [waku[u] for u in box]
        bet = {frozenset(c) for c in combinations(set(brs), 2)}
        for b in set(brs):
            if brs.count(b) >= 2:
                bet.add(frozenset({b}))
    else:
        return None
    if not bet:
        return None  # 上位頭数不足等でboxが作れない＝賭け不能
    ret = sum(mult for combo, mult in wins if combo in bet)
    return len(bet), ret


def agg_cell(
    picks: dict,
    payoffs: dict,
    wakuban: dict,
    bet_key: str,
    kind: str,
    n: int,
    y0: int,
    y1: int,
) -> dict:
    """年範囲[y0,y1]で1セル（券種×N）を集計。{n(賭け可能race), stake, ret, hit}。"""
    n_bet = n_hit = 0
    stake = ret = 0.0
    for rid, info in picks.items():
        if not (y0 <= info["year"] <= y1):
            continue
        wins = payoffs.get(rid, {}).get(bet_key)
        if wins is None:
            continue  # 非発売
        res = _eval_race(kind, info["order"], n, wins, wakuban.get(rid))
        if res is None:
            continue
        s, r = res
        n_bet += 1
        stake += s
        ret += r
        if r > 0:
            n_hit += 1
    return {"n": n_bet, "stake": stake, "ret": ret, "hit_n": n_hit}


def _roi(c: dict) -> float:
    return c["ret"] / c["stake"] if c["stake"] else 0.0


def _hit(c: dict) -> float:
    return c["hit_n"] / c["n"] if c["n"] else 0.0


def tansho_baseline(picks: dict, y0: int, y1: int) -> dict:
    """単勝N=1基準: キャッシュの単勝オッズで Top-1 単勝（run_backtest と同一定義）。"""
    n = wins = 0
    ret = 0.0
    for info in picks.values():
        if not (y0 <= info["year"] <= y1):
            continue
        if info["top1_odds"] is not None:
            n += 1
            if info["top1_won"] == 1:
                wins += 1
                ret += info["top1_odds"]
    return {"n": n, "stake": float(n), "ret": ret, "hit_n": wins}


# ============================================================
# Part 1 / Part 2
# ============================================================
def cells():
    """事前登録グリッドを (券種キー, 表示名, 種別, N) で列挙。"""
    for key, label, kind, ns in GRID:
        for n in ns:
            yield key, label, kind, n


def run_part1(picks: dict, payoffs: dict, wakuban: dict, n_is: int, label: str) -> dict:
    """IS全体で各セルを集計し表出力。返り値は JSON 用 dict。"""
    logger.info(f"=== Part 1 診断: IS {IS_START}-{IS_END}（配点: {label}） ===")
    base = tansho_baseline(picks, IS_START, IS_END)
    logger.info(f"  {'券種':<8}{'N':>3}{'賭可能':>9}{'被覆':>7}{'的中率':>9}{'ROI':>9}")
    logger.info(
        f"  {'単勝(基準)':<8}{1:>3}{base['n']:>9,}{base['n'] / n_is * 100:>6.0f}%"
        f"{_hit(base) * 100:>8.1f}%{_roi(base) * 100:>8.1f}%"
    )
    out = {"tansho_baseline": base, "cells": {}}
    for key, name, kind, n in cells():
        c = agg_cell(picks, payoffs, wakuban, key, kind, n, IS_START, IS_END)
        out["cells"][f"{key}_N{n}"] = c
        cov = c["n"] / n_is * 100 if n_is else 0.0
        logger.info(
            f"  {name:<8}{n:>3}{c['n']:>9,}{cov:>6.0f}%{_hit(c) * 100:>8.1f}%{_roi(c) * 100:>8.1f}%"
        )
    return out


def run_part2(picks: dict, payoffs: dict, wakuban: dict, min_n: int) -> dict:
    """IS内CV。各セルの検証ROIを3fold横断でプール＋学習最良セルの選択回帰。"""
    logger.info(
        f"=== Part 2 IS内CV（{len(CV_FOLDS)}fold・学習{min_n}件未満のfoldは除外） ==="
    )
    cell_list = list(cells())
    # 各セル: foldごとに (train, valid) を集計。train>=min_n の fold のみ valid をプール。
    pooled: dict[str, dict] = {}
    for key, name, kind, n in cell_list:
        ck = f"{key}_N{n}"
        v_stake = v_ret = v_n = 0.0
        t_stake_w = t_ret_w = 0.0  # 採用foldのtrainプール（gap算出用）
        used_folds = 0
        for tr_s, tr_e, va_s, va_e in CV_FOLDS:
            tr = agg_cell(picks, payoffs, wakuban, key, kind, n, tr_s, tr_e)
            if tr["n"] < min_n:
                continue
            va = agg_cell(picks, payoffs, wakuban, key, kind, n, va_s, va_e)
            v_stake += va["stake"]
            v_ret += va["ret"]
            v_n += va["n"]
            t_stake_w += tr["stake"]
            t_ret_w += tr["ret"]
            used_folds += 1
        # データ無しは NaN でなく None（NaN は標準JSON外。出力は allow_nan=False で検出）。
        roi_v = v_ret / v_stake if v_stake else None
        roi_t = t_ret_w / t_stake_w if t_stake_w else None
        pooled[ck] = {
            "name": name,
            "n": n,
            "used_folds": used_folds,
            "valid_n": int(v_n),
            "valid_roi": roi_v,
            "train_roi": roi_t,
            "gap": (roi_t - roi_v)
            if (roi_t is not None and roi_v is not None)
            else None,
        }
    # セル別 検証ROI（過学習補正後の素直な実力）を降順表示。
    logger.info("  ◆ セル別 検証ROI（事前登録ゆえ選択バイアス無し）")
    logger.info(
        f"  {'セル':<14}{'fold':>5}{'検証N':>8}{'学習ROI':>9}{'検証ROI':>9}{'gap':>8}"
    )

    def _pct(v: float | None) -> str:
        return f"{v * 100:.1f}%" if v is not None else "—"

    for ck, p in sorted(
        pooled.items(),
        key=lambda kv: kv[1]["valid_roi"] if kv[1]["valid_roi"] is not None else -1.0,
        reverse=True,
    ):
        if p["used_folds"] == 0:
            logger.info(f"  {ck:<14}{'—':>5}  (学習不足で全fold除外)")
            continue
        gap_s = f"{p['gap'] * 100:+.1f}" if p["gap"] is not None else "—"
        logger.info(
            f"  {ck:<14}{p['used_folds']:>5}{p['valid_n']:>8,}"
            f"{_pct(p['train_roi']):>9}{_pct(p['valid_roi']):>9}{gap_s:>8}"
        )
    # 選択回帰: foldごとに学習ROI最良セルを選び検証で採点 → プール。
    sel = _selection_regression(picks, payoffs, wakuban, cell_list, min_n)
    return {"by_cell": pooled, "selection_regression": sel}


def _selection_regression(picks, payoffs, wakuban, cell_list, min_n) -> dict:
    """各foldで学習ROI最良セル（train>=min_n）を選び、検証ROIをプール＝選択バイアスの量。"""
    folds = []
    v_stake = v_ret = 0.0
    for tr_s, tr_e, va_s, va_e in CV_FOLDS:
        best = None
        for key, _name, kind, n in cell_list:
            tr = agg_cell(picks, payoffs, wakuban, key, kind, n, tr_s, tr_e)
            if tr["n"] < min_n:
                continue
            roi_t = _roi(tr)
            if best is None or roi_t > best[1]:
                best = (f"{key}_N{n}", roi_t, key, kind, n)
        if best is None:
            folds.append({"valid": [va_s, va_e], "selected": None})
            continue
        va = agg_cell(picks, payoffs, wakuban, best[2], best[3], best[4], va_s, va_e)
        v_stake += va["stake"]
        v_ret += va["ret"]
        folds.append(
            {
                "valid": [va_s, va_e],
                "selected": best[0],
                "train_roi": best[1],
                "valid_roi": _roi(va),
                "valid_n": va["n"],
            }
        )
        logger.info(
            f"  選択回帰 検証{va_s}-{va_e}: 学習最良={best[0]}(学習ROI {best[1] * 100:.1f}%)"
            f" → 検証ROI {_roi(va) * 100:.1f}% (N={va['n']})"
        )
    overall = v_ret / v_stake if v_stake else None
    overall_s = f"{overall * 100:.1f}%" if overall is not None else "—"
    logger.info(f"  → 選択回帰 検証ROIプール（最良券種の実力見積もり）: {overall_s}")
    return {"folds": folds, "valid_roi_pooled": overall}


def _sanity_check(races: dict, picks_default: dict) -> None:
    """単勝N=1 が run_backtest の IS ROI を再現することを確認（再利用ロジックの担保）。"""
    ref = evaluate(races, IS_START, IS_END, DEFAULT_WEIGHTS, DEFAULT_WR_BLEND)
    mine = tansho_baseline(picks_default, IS_START, IS_END)
    mine_roi = _roi(mine)
    ok = abs(mine_roi - ref["roi"]) < 1e-6 and mine["n"] == ref["n"]
    logger.info(
        f"[サニティ] 単勝N=1 ROI {mine_roi * 100:.4f}% / run_backtest {ref['roi'] * 100:.4f}%"
        f" / N {mine['n']}={ref['n']} → {'OK' if ok else '不一致!'}"
    )
    if not ok:
        logger.warning("サニティ不一致: 再利用ロジックを確認すること")


def main() -> None:
    ap = argparse.ArgumentParser(description="#B5 券種別ボックス診断（順序なし6券種）")
    ap.add_argument(
        "--min-n",
        type=int,
        default=30,
        help="CVで採用するfoldの学習最小ベット数（既定30）",
    )
    ap.add_argument(
        "--smoke", action="store_true", help="狭い年範囲で実行可否のみ確認（CV省略）"
    )
    args = ap.parse_args()

    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    try:
        races = load_races(conn)
        is_races = {
            rid: r
            for rid, r in races.items()
            if IS_START <= r[0]["as_of_year"] <= IS_END
        }
        race_set = set(is_races)
        logger.info(f"IS対象: {len(race_set):,} レース")
        payoffs = load_payoffs(conn, race_set)
        wakuban = load_wakuban(conn, race_set)
        logger.info(
            f"払戻ロード: {len(payoffs):,} race / 枠番ロード: {len(wakuban):,} race"
        )
    finally:
        conn.close()

    # Stage D 感度用の重み（IS適合済み＝楽観の天井値）。
    stage_d = None
    if TOP1_WEIGHTS_PATH.exists():
        td = json.loads(TOP1_WEIGHTS_PATH.read_text(encoding="utf-8"))
        stage_d = (td["weights"], td["wr_blend"])

    n_is = len(race_set)
    picks_default = build_picks(races, DEFAULT_WEIGHTS, DEFAULT_WR_BLEND)
    _sanity_check(races, picks_default)

    if args.smoke:
        logger.info("[smoke] 2005-2008 の一部セルのみ確認（結果の良否は判断しない）")
        for key, name, kind, n in [
            ("fukusho", "複勝", "place", 3),
            ("wakuren", "枠連", "bracket", 3),
            ("sanrenpuku", "3連複", "triple", 3),
        ]:
            c = agg_cell(picks_default, payoffs, wakuban, key, kind, n, 2005, 2008)
            logger.info(
                f"  {name} N{n}: 賭可能{c['n']} 的中率{_hit(c) * 100:.1f}% ROI{_roi(c) * 100:.1f}%"
            )
        return

    part1_default = run_part1(picks_default, payoffs, wakuban, n_is, "DEFAULT(本命)")
    part1_staged = None
    if stage_d:
        picks_staged = build_picks(races, stage_d[0], stage_d[1])
        part1_staged = run_part1(
            picks_staged, payoffs, wakuban, n_is, "Stage D Top-1（IS適合=楽観・参考）"
        )
    part2 = run_part2(picks_default, payoffs, wakuban, args.min_n)

    out = {
        "is_period": [IS_START, IS_END],
        "min_n": args.min_n,
        "n_is_races": n_is,
        "grid": {label: ns for _, label, _, ns in GRID},
        "part1_default": part1_default,
        "part1_stage_d": part1_staged,
        "part2_cv_default": part2,
        "note": "計測装置。CodeRabbitレビュー通過まで数値は暫定。OOS封印。",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT_PATH.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    logger.info(f"レポート保存: {OUT_PATH}")


if __name__ == "__main__":
    main()

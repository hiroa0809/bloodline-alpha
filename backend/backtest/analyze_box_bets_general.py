"""Direction A 追補: 一般戦×スピード/全部入りの「賭け方」横断診断（計測装置・本番前）。

ユーザー問い「スピード指数 vs 全部入り、どちらが好成績か」を、単勝1点だけでなく
複数頭・複数券種・オッズ下限まで広げて測る。これまでの券種診断（analyze_bet_types.py＝
新馬戦×ファンダ）と違い、土俵を一般戦・軸をスピード指数に置き、3連単（順序付き）と
枠連まで含めて「賭けの構造を変えれば控除（~20%）を越えて黒字化できるか」を横断探索する。

金庫ルール（CLAUDE.md「バックテスト方法論」）厳守:
  - 探索は IS（1993-2013）限定。OOS は load_arrays(year_max=IS_END) で SQL 封印。
  - スコアは固定（再最適化しない）。スピード=sp_soha_recent / 全部入り=top1_weights_general.json /
    市場=人気。券種・N・オッズ下限は事前登録グリッド（結果を見て動かさない）。
  - 当たり判定は払戻表（jvd_haraimodoshi）の当選エントリを真実とする（着順から自前復元しない）。
    非発売（3連複02年〜/3連単04年〜/ワイド99年〜＝該当年JSONが null）は被覆に反映される。

4部構成（すべて IS 全体の暫定診断。OOS は触らない）:
  P1 precision@5 — 各レース上位5頭を選び「確定5着以内」に何頭入るか（生の的中力）。
  P2 オッズ妙味 — 上位5頭の平均オッズ・1レースオッズ合計・単勝ROI・入賞馬の平均オッズ。
  P3 単勝N点買い — 上位N頭に単勝1点ずつ（N=2..6）。オッズ下限（1番人気≥N倍）あり/なし。
  P4 券種別ボックス — 複勝/馬連/ワイド/枠連/3連複/3連単（N=2..6）。下限あり/なし。

使い方:
    python backend/backtest/analyze_box_bets_general.py
    python backend/backtest/analyze_box_bets_general.py --smoke   # 狭年範囲で実行可否のみ確認
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from itertools import combinations, permutations
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from backtest.top1_core import (  # noqa: E402  （PR#12 でレビュー済みの再利用部品）
    CACHE_GENERAL,
    load_arrays,
    score_all,
)

DB_PATH = _BACKEND_DIR / "bloodline.db"
OUT_PATH = _BACKEND_DIR / "backtest" / "box_bets_general_report.json"
GEN_WEIGHTS_PATH = _BACKEND_DIR / "backtest" / "top1_weights_general.json"

# IS（学習区間。CLAUDE.md「バックテスト方法論」）。OOS は封印。
IS_START, IS_END = 1993, 2013
MIN_FIELD = 8  # 出走頭数の下限（上位5/6頭の選別が意味を持つ最小規模）
TOP5 = 5

# 事前登録グリッド: (払戻列名, 表示名, 種別, N候補)。
#   place=複勝(各馬), pair=馬連/ワイド(C(N,2)), bracket=枠連(枠ペア),
#   triple=3連複(C(N,3)), perm3=3連単(順序付き P(N,3))。
GRID = [
    ("fukusho", "複勝", "place", [2, 3, 4, 5, 6]),
    ("umaren", "馬連", "pair", [2, 3, 4, 5, 6]),
    ("wide", "ワイド", "pair", [2, 3, 4, 5, 6]),
    ("wakuren", "枠連", "bracket", [2, 3, 4, 5, 6]),
    ("sanrenpuku", "3連複", "triple", [3, 4, 5, 6]),
    ("sanrentan", "3連単", "perm3", [3, 4, 5, 6]),
]
# 払戻 kumiban のパース位置（開始, 長さ）。3連単のみ順序保持タプルで別扱い。
_KUMI_POS = {
    "wakuren": [(0, 1), (1, 1)],
    "umaren": [(0, 2), (2, 2)],
    "wide": [(0, 2), (2, 2)],
    "sanrenpuku": [(0, 2), (2, 2), (4, 2)],
}
METHODS = ("speed", "full", "market")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ============================================================
# 払戻のパース（analyze_bet_types.py PR#15 と同一方針＋3連単を追加）
# ============================================================
def _loads(s: str | None) -> list:
    """払戻列をリスト化。DB は Python repr 格納だが json も試し、失敗時 literal_eval。"""
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
    """複勝 [{umaban, haraimodoshi_kin}, ...] → [(frozenset({馬番}), 倍率)]。"""
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
    """連系（順序なし）[{kumiban, haraimodoshi_kin}, ...] → [(frozenset({番...}), 倍率)]。"""
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


def _parse_tan3(js: str) -> list[tuple[tuple[int, int, int], float]]:
    """3連単 [{kumiban(6桁), haraimodoshi_kin}, ...] → [((1着,2着,3着), 倍率)]。順序を保持。"""
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
            t = (int(kumi[0:2]), int(kumi[2:4]), int(kumi[4:6]))
        except (ValueError, TypeError, IndexError):
            continue
        if all(n > 0 for n in t):
            out.append((t, kin / 100.0))
    return out


def _race_id(nen, th, kj, kai, ni, rb) -> str:
    """precompute と同一の race_id 連結（払戻PK・race_uma から復元）。"""
    return f"{nen}{th}{kj}{kai}{ni}{rb}"


def load_payoffs(conn: sqlite3.Connection, race_set: set[str]) -> dict[str, dict]:
    """対象 race の払戻を {race_id: {券種: 当選エントリ列}} で返す（非発売はキー無し）。"""
    cur = conn.execute(
        "SELECT kaisai_nen,kaisai_tsukihi,keibajo_code,kaisai_kai,kaisai_nichime,race_bango,"
        "fukusho,wakuren,umaren,wide,sanrenpuku,sanrentan FROM jvd_haraimodoshi "
        "WHERE kaisai_nen BETWEEN ? AND ?",
        (str(IS_START), str(IS_END)),
    )
    payoffs: dict[str, dict] = {}
    for nen, th, kj, kai, ni, rb, fuku, wk, ur, wd, sp3, st3 in cur:
        rid = _race_id(nen, th, kj, kai, ni, rb)
        if rid not in race_set:
            continue
        d: dict = {}
        if fuku:
            ent = _parse_single(fuku)
            if ent:
                d["fukusho"] = ent
        for col, js in (
            ("wakuren", wk),
            ("umaren", ur),
            ("wide", wd),
            ("sanrenpuku", sp3),
        ):
            if js:
                ent = _parse_kumi(js, _KUMI_POS[col])
                if ent:
                    d[col] = ent
        if st3:
            ent = _parse_tan3(st3)
            if ent:
                d["sanrentan"] = ent
        if d:
            payoffs[rid] = d
    return payoffs


def load_wakuban(
    conn: sqlite3.Connection, race_set: set[str]
) -> dict[str, dict[int, int]]:
    """対象 race の枠番を {race_id: {馬番: 枠番}} で返す（枠連box構築用）。"""
    cur = conn.execute(
        "SELECT kaisai_nen,kaisai_tsukihi,keibajo_code,kaisai_kai,kaisai_nichime,race_bango,"
        "umaban,wakuban FROM jvd_race_uma WHERE kaisai_nen BETWEEN ? AND ?",
        (str(IS_START), str(IS_END)),
    )
    wk: dict[str, dict[int, int]] = {}
    for nen, th, kj, kai, ni, rb, umaban, wakuban in cur:
        rid = _race_id(nen, th, kj, kai, ni, rb)
        if rid not in race_set:
            continue
        try:
            u, b = int(umaban), int(wakuban)
        except (TypeError, ValueError):
            continue
        if u > 0 and b > 0:
            wk.setdefault(rid, {})[u] = b
    return wk


# ============================================================
# レース・picks 構築（スコア固定で上位順を並べる）
# ============================================================
def _ban(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return -1


def build_races(a: dict, full_score: np.ndarray, y0: int, y1: int) -> dict[str, dict]:
    """[y0,y1]・出走MIN_FIELD頭以上の各レースで上位順（馬番列）と着内/オッズ等を返す。

    speed=sp_soha_recent 降順（指数を持つ馬のみ）/ full=総合スコア降順 / market=人気昇順。
    同点はキャッシュの (as_of_year, race_id, umaban) 昇順を numpy stable sort が保持する。
    """
    spd = a["sp_soha_recent"]
    nk = a["ninki"]
    od = a["odds"]
    ck = a["chaku"]
    wn = a["won"]
    ub = a["umaban"]
    starts, ryear, R, rid_arr = a["race_start"], a["race_year"], a["R"], a["race_id"]
    races: dict[str, dict] = {}
    for r in range(R):
        if not (y0 <= ryear[r] <= y1):
            continue
        s, e = starts[r], starts[r + 1]
        if e - s < MIN_FIELD:
            continue
        sp_l, nk_l, od_l = spd[s:e], nk[s:e], od[s:e]
        ck_l, wn_l, fs_l = ck[s:e], wn[s:e], full_score[s:e]
        ban_l = np.array([_ban(x) for x in ub[s:e]])
        spm = ~np.isnan(sp_l)
        sp_order = np.nonzero(spm)[0][np.argsort(-sp_l[spm], kind="stable")]
        full_order = np.argsort(-fs_l, kind="stable")
        mk_idx = np.nonzero(nk_l >= 1)[0]
        mk_order = mk_idx[np.argsort(nk_l[mk_idx], kind="stable")]
        fav = np.nonzero(nk_l == 1)[0]
        fav_odds = (
            float(od_l[fav[0]]) if len(fav) and not np.isnan(od_l[fav[0]]) else None
        )
        rid = str(rid_arr[s])
        races[rid] = {
            "order": {
                "speed": ban_l[sp_order],
                "full": ban_l[full_order],
                "market": ban_l[mk_order],
            },
            "idx": {  # ローカル index 列（着内/オッズ集計用・P1/P2）
                "speed": sp_order,
                "full": full_order,
                "market": mk_order,
            },
            "nsp": int(spm.sum()),
            "fav_odds": fav_odds,
            "N": int(e - s),
            "chaku": ck_l,
            "won": wn_l,
            "odds": od_l,
        }
    return races


# ============================================================
# P1 precision@5 / P2 オッズ妙味
# ============================================================
def part1_precision5(races: dict) -> dict:
    """上位5頭のうち確定5着以内（1..5）に入る頭数の割合（公平条件: スピード5頭以上）。"""
    agg = {m: {"hits": 0, "picks": 0, "races": 0} for m in METHODS}
    rand_hits = 0.0
    rand_picks = 0
    for info in races.values():
        if info["nsp"] < TOP5:
            continue
        in5 = (info["chaku"] >= 1) & (info["chaku"] <= 5)
        T = int(in5.sum())
        for m in METHODS:
            idx = info["idx"][m][:TOP5]
            if len(idx) < TOP5:
                continue
            agg[m]["hits"] += int(in5[idx].sum())
            agg[m]["picks"] += TOP5
            agg[m]["races"] += 1
        rand_hits += TOP5 * T / info["N"]
        rand_picks += TOP5
    out = {
        m: {
            "precision5": agg[m]["hits"] / agg[m]["picks"] if agg[m]["picks"] else None,
            "avg_in5": agg[m]["hits"] / agg[m]["races"] if agg[m]["races"] else None,
            "races": agg[m]["races"],
        }
        for m in METHODS
    }
    out["random"] = {
        "precision5": rand_hits / rand_picks if rand_picks else None,
        "races": agg["speed"]["races"],
    }
    return out


def part2_odds_value(races: dict) -> dict:
    """上位5頭の平均オッズ・1レースオッズ合計・単勝5点ROI・入賞馬の平均オッズ。"""
    agg = {
        m: {
            "odds_sum": 0.0,
            "odds_n": 0,
            "pay": 0.0,
            "race_tot": [],
            "hit_odds": 0.0,
            "hit_n": 0,
        }
        for m in METHODS
    }
    for info in races.values():
        if info["nsp"] < TOP5:
            continue
        in5 = (info["chaku"] >= 1) & (info["chaku"] <= 5)
        od, wn = info["odds"], info["won"]
        for m in METHODS:
            idx = info["idx"][m][:TOP5]
            if len(idx) < TOP5:
                continue
            po = od[idx]
            pv = ~np.isnan(po)
            d = agg[m]
            d["odds_sum"] += float(np.nansum(po))
            d["odds_n"] += int(pv.sum())
            d["race_tot"].append(float(np.nansum(po)))
            d["pay"] += float(np.nansum(po * wn[idx]))  # 単勝5点: 当たり馬のオッズ
            hm = in5[idx] & pv
            d["hit_odds"] += float(np.nansum(po[hm]))
            d["hit_n"] += int(hm.sum())
    out = {}
    for m in METHODS:
        d = agg[m]
        out[m] = {
            "avg_odds": d["odds_sum"] / d["odds_n"] if d["odds_n"] else None,
            "per_race_odds_total": float(np.mean(d["race_tot"]))
            if d["race_tot"]
            else None,
            "tansho5_roi": d["pay"] / d["odds_n"] if d["odds_n"] else None,
            "hit_avg_odds": d["hit_odds"] / d["hit_n"] if d["hit_n"] else None,
        }
    return out


# ============================================================
# P3 単勝N点買い（オッズ下限あり/なし）
# ============================================================
def part3_tansho_npoint(races: dict, floor: bool) -> dict:
    """上位N頭に単勝1点ずつ（N=2..6）。floor=True は 1番人気 ≥ N倍 のレースのみ。"""
    out = {}
    for N in range(2, 7):
        agg = {m: {"pay": 0.0, "stake": 0, "hit": 0, "races": 0} for m in METHODS}
        for info in races.values():
            if info["nsp"] < N:
                continue
            if floor and (info["fav_odds"] is None or info["fav_odds"] < N):
                continue
            od, wn = info["odds"], info["won"]
            for m in METHODS:
                idx = info["idx"][m][:N]
                if len(idx) < N:
                    continue
                pay = float(np.nansum(od[idx] * wn[idx]))
                d = agg[m]
                d["pay"] += pay
                d["stake"] += N
                d["races"] += 1
                if np.nansum(wn[idx]) > 0:
                    d["hit"] += 1
        out[f"N{N}"] = {
            m: {
                "roi": agg[m]["pay"] / agg[m]["stake"] if agg[m]["stake"] else None,
                "hit_rate": agg[m]["hit"] / agg[m]["races"]
                if agg[m]["races"]
                else None,
                "races": agg[m]["races"],
            }
            for m in METHODS
        }
    return out


# ============================================================
# P4 券種別ボックス（オッズ下限あり/なし・払戻表駆動）
# ============================================================
def _make_bet(kind: str, box, waku: dict | None):
    """上位N頭の馬番列 box から、券種別の賭け組合せ集合を作る。賭け不能なら None。"""
    box = list(box)
    if kind == "place":
        return {frozenset({u}) for u in box}
    if kind == "pair":
        return {frozenset(c) for c in combinations(box, 2)}
    if kind == "triple":
        return {frozenset(c) for c in combinations(box, 3)}
    if kind == "perm3":
        return set(permutations(box, 3))  # 順序付きタプル（3連単）
    if kind == "bracket":
        if waku is None or any(u not in waku for u in box):
            return None  # 上位N頭の枠番が揃わない＝意図したboxを作れない
        brs = [waku[u] for u in box]
        bet = {frozenset(c) for c in combinations(set(brs), 2)}
        for b in set(brs):
            if brs.count(b) >= 2:
                bet.add(frozenset({b}))
        return bet
    return None


def _eval_cell(races, payoffs, wakuban, key, kind, N, floor) -> dict:
    """1セル（券種×N×下限）を集計。stake=Σ組合せ数, ret=Σ払戻倍率, n=賭け可能race。"""
    agg = {m: {"stake": 0.0, "ret": 0.0, "n": 0, "hit": 0} for m in METHODS}
    for rid, info in races.items():
        if info["nsp"] < N:
            continue
        if floor and (info["fav_odds"] is None or info["fav_odds"] < N):
            continue
        wins = payoffs.get(rid, {}).get(key)
        if wins is None:
            continue  # 非発売
        waku = wakuban.get(rid)
        for m in METHODS:
            box = info["order"][m][:N]
            if len(box) < N:
                continue
            bet = _make_bet(kind, box, waku)
            if not bet:
                continue
            ret = sum(mult for combo, mult in wins if combo in bet)
            d = agg[m]
            d["stake"] += len(bet)
            d["ret"] += ret
            d["n"] += 1
            if ret > 0:
                d["hit"] += 1
    return agg


def part4_box(races, payoffs, wakuban, floor: bool) -> dict:
    out = {}
    for key, _name, kind, Ns in GRID:
        for N in Ns:
            agg = _eval_cell(races, payoffs, wakuban, key, kind, N, floor)
            out[f"{key}_N{N}"] = {
                m: {
                    "roi": agg[m]["ret"] / agg[m]["stake"] if agg[m]["stake"] else None,
                    "hit_rate": agg[m]["hit"] / agg[m]["n"] if agg[m]["n"] else None,
                    "races": agg[m]["n"],
                }
                for m in METHODS
            }
    return out


# ============================================================
# 出力
# ============================================================
def _pct(v) -> str:
    return f"{v * 100:5.1f}%" if v is not None else "  —  "


def _log_p1_p2(p1: dict, p2: dict) -> None:
    logger.info(
        f"=== P1 precision@5（上位5頭の確定5着以内入賞率・races={p1['speed']['races']:,}） ==="
    )
    for m in METHODS:
        d = p1[m]
        logger.info(
            f"  {m:<7}{_pct(d['precision5'])}  (1レース平均 {d['avg_in5']:.3f}/5頭)"
        )
    logger.info(f"  random {_pct(p1['random']['precision5'])}  (期待値ベースライン)")
    logger.info("=== P2 オッズ妙味（上位5頭） ===")
    for m in METHODS:
        d = p2[m]
        logger.info(
            f"  {m:<7}平均オッズ={d['avg_odds']:6.2f}倍 1レース合計={d['per_race_odds_total']:7.2f}倍 "
            f"単勝5点ROI={_pct(d['tansho5_roi'])} 入賞馬平均={d['hit_avg_odds']:6.2f}倍"
        )


def _log_tansho(p3_off: dict, p3_on: dict) -> None:
    logger.info(
        "=== P3 単勝N点買い ROI（speed/full/market）  [下限なし | 下限あり(1人気≥N倍)] ==="
    )
    for N in range(2, 7):
        a, b = p3_off[f"N{N}"], p3_on[f"N{N}"]
        off = "/".join(_pct(a[m]["roi"]) for m in METHODS)
        on = "/".join(_pct(b[m]["roi"]) for m in METHODS)
        logger.info(
            f"  N={N}: {off} ({a['speed']['races']:,})  |  {on} ({b['speed']['races']:,})"
        )


def _log_box(p4_off: dict, p4_on: dict) -> None:
    logger.info(
        "=== P4 券種別ボックス ROI（speed/full/market）  [下限なし | 下限あり(1人気≥N倍)] ==="
    )
    for key, name, _kind, Ns in GRID:
        logger.info(f"  --- {name} ---")
        for N in Ns:
            ck = f"{key}_N{N}"
            a, b = p4_off[ck], p4_on[ck]
            off = "/".join(_pct(a[m]["roi"]) for m in METHODS)
            on = "/".join(_pct(b[m]["roi"]) for m in METHODS)
            logger.info(
                f"    N={N}: {off} ({a['speed']['races']:,})  |  {on} ({b['speed']['races']:,})"
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="狭年範囲で実行可否のみ確認")
    args = ap.parse_args()
    y0, y1 = (2010, 2013) if args.smoke else (IS_START, IS_END)

    conn = sqlite3.connect(DB_PATH)
    # OOS を SQL で封印（year_max=IS_END）。スコアは固定（再最適化しない）。
    a = load_arrays(conn, CACHE_GENERAL, year_max=IS_END)
    weights = json.loads(GEN_WEIGHTS_PATH.read_text(encoding="utf-8"))
    full_score = score_all(a, weights["weights"], weights["wr_blend"])
    races = build_races(a, full_score, y0, y1)
    race_set = set(races.keys())
    payoffs = load_payoffs(conn, race_set)
    wakuban = load_wakuban(conn, race_set)
    conn.close()
    logger.info(f"対象レース: {len(races):,}（{y0}-{y1}・出走{MIN_FIELD}頭以上）")

    p1 = part1_precision5(races)
    p2 = part2_odds_value(races)
    p3_off = part3_tansho_npoint(races, floor=False)
    p3_on = part3_tansho_npoint(races, floor=True)
    p4_off = part4_box(races, payoffs, wakuban, floor=False)
    p4_on = part4_box(races, payoffs, wakuban, floor=True)

    _log_p1_p2(p1, p2)
    _log_tansho(p3_off, p3_on)
    _log_box(p4_off, p4_on)

    report = {
        "meta": {
            "scope": "general-races (non-maiden flat)",
            "is_range": [y0, y1],
            "min_field": MIN_FIELD,
            "smoke": args.smoke,
            "speed_feature": "sp_soha_recent",
            "full_weights": "top1_weights_general.json",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "note": "金庫ルール厳守・OOS封印・スコア固定・事前登録グリッド。暫定（CodeRabbit前）。",
        },
        "p1_precision5": p1,
        "p2_odds_value": p2,
        "p3_tansho_npoint": {"floor_off": p3_off, "floor_on": p3_on},
        "p4_box": {"floor_off": p4_off, "floor_on": p4_on},
    }
    OUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    logger.info(f"レポート出力: {OUT_PATH}")


if __name__ == "__main__":
    main()

"""point-in-time（as-of）サブスコア前計算バッチ（Phase 1-B2 / #B1）。

新馬戦701（クリーン期間 1986〜2025）の各出走馬について、対象レース日より前の
データ「のみ」で算出したサブスコアのパーセンタイル（勝率pctl / ROIpctl を分離）を
前計算し、backtest_subscore_cache テーブルへ格納する。

なぜ必要か（CLAUDE.md「バックテスト方法論」）:
  現行の集計バッチ（calc_*_stats.py）は全期間一括集計のため、対象レースより後の
  結果を含む＝バックテストではデータリーク。本バッチは年次チェックポイント方式の
  as-of 集計で「未来を見ない」サブスコアを作る。重み非依存のため一度だけ前計算し、
  #B3 の重み最適化は『重み × 本キャッシュ』のベクトル演算だけで済む。

設計:
  - 年次チェックポイント: 各年 Y の新馬戦は『Y より前の年』の累積統計でスコア化
    （未来リークはゼロ。陳腐化は最大 ~14ヶ月だが種牡馬成績は緩慢なため実害小）。
  - 母集団定義はライブの各 *_score.py と厳密一致（しきい値・条件区分・回収率式）。
    唯一の違いは「対象年より前のみ」。draw/weight(E1/E2)は新馬戦701のみで集計。

使い方:
    python backend/backtest/precompute_subscores.py
    python backend/backtest/precompute_subscores.py --start-year 2014 --end-year 2015  # 部分検証
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# app.services の純粋関数（A4/A5: 血統決定論・リーク無し）を再利用
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
from app.services.bloodline_score import (  # noqa: E402
    _calc_inbreed_score,
    parse_sandai_ketto_full,
)

from backtest.asof_helpers import (  # noqa: E402
    THRESHOLDS,
    AsOfStats,
    feature,
    kyori_to_distance_band,
    resolve_going,
    roi_numerator,
    track_to_surface,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

DB_PATH = _BACKEND_DIR / "bloodline.db"

# クリーン期間（CLAUDE.md バックテスト方法論）
CLEAN_START = 1986
CLEAN_END = 2025

# 出力テーブル
CACHE_TABLE = "backtest_subscore_cache"

# 特徴量カラム（勝率pctl / ROIpctl を分離）
FEAT_COLS = [
    "a1_wr",
    "a1_roi",  # A1 父
    "a2_wr",
    "a2_roi",  # A2 母父
    "a3_wr",
    "a3_roi",  # A3 ニックス
    "b1_sire_wr",
    "b1_sire_roi",
    "b1_bms_wr",
    "b1_bms_roi",  # B1 馬場
    "b2_sire_wr",
    "b2_sire_roi",
    "b2_bms_wr",
    "b2_bms_roi",  # B2 距離
    "b3_sire_wr",
    "b3_sire_roi",
    "b3_bms_wr",
    "b3_bms_roi",  # B3 開催地
    "b4_sire_wr",
    "b4_sire_roi",
    "b4_bms_wr",
    "b4_bms_roi",  # B4 馬場状態
    "c1_wr",
    "c1_roi",  # C1 調教師
    "c2_wr",
    "c2_roi",  # C2 騎手
    "c3_owner_wr",
    "c3_owner_roi",
    "c3_breeder_wr",
    "c3_breeder_roi",  # C3 馬主/生産者
    "e1_wr",
    "e1_roi",  # E1 枠順
    "e2_wr",
    "e2_roi",  # E2 斤量
]


# ============================================================
# マスタ・レース情報のメモリロード
# ============================================================


def _parse_sandai_list(raw: str | None):
    """sandai_ketto 文字列 → list or None（ライブ parse と同方針）。"""
    if not raw:
        return None
    try:
        try:
            lst = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            lst = ast.literal_eval(raw)
        return lst if isinstance(lst, list) else None
    except (ValueError, SyntaxError, TypeError, RecursionError, MemoryError):
        return None


def load_pedigree_maps(conn: sqlite3.Connection):
    """jvd_uma から 父/母父・A4/A5・生産者 のメモリマップを構築。"""
    logger.info("血統・生産者マップを構築中...")
    sire_map: dict[str, tuple[str | None, str | None]] = {}
    a4a5_map: dict[str, tuple[float | None, int | None]] = {}
    seisansha_map: dict[str, str | None] = {}

    cur = conn.execute(
        "SELECT ketto_toroku_bango, sandai_ketto, seisansha_code FROM jvd_uma"
    )
    for ketto, sandai_raw, seisansha in cur:
        seisansha_map[ketto] = (seisansha or "").strip() or None
        lst = _parse_sandai_list(sandai_raw)
        sire_b = bms_b = None
        if lst:
            if len(lst) > 0 and isinstance(lst[0], dict):
                sire_b = (lst[0].get("hanshoku_toroku_bango") or "").strip() or None
            if len(lst) > 4 and isinstance(lst[4], dict):
                bms_b = (lst[4].get("hanshoku_toroku_bango") or "").strip() or None
        sire_map[ketto] = (sire_b, bms_b)

        # A4/A5: 14頭分の dict が揃っている時だけ算出（ライブと同一の検証）
        coi_val: float | None = None
        outbreed: int | None = None
        full = parse_sandai_ketto_full(sandai_raw)
        if full and len(full) >= 14:
            first14 = full[:14]
            if all(
                isinstance(e, dict) and (e.get("hanshoku_toroku_bango") or "").strip()
                for e in first14
            ):
                _, coi_val, _ = _calc_inbreed_score(first14, 1.0)
                outbreed = 1 if coi_val == 0.0 else 0
        a4a5_map[ketto] = (coi_val, outbreed)

    logger.info(f"  jvd_uma: {len(sire_map):,} 頭")
    return sire_map, a4a5_map, seisansha_map


def load_race_info(conn: sqlite3.Connection):
    """jvd_race から レースPK → 条件情報 のマップを構築。"""
    logger.info("レース条件マップを構築中...")
    race_info: dict[tuple, dict] = {}
    cur = conn.execute(
        "SELECT kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, "
        "  race_bango, track_code, kyori, shiba_baba_jotai_code, dirt_baba_jotai_code, "
        "  kyoso_joken_code_2sai, kyoso_joken_code_3sai "
        "FROM jvd_race"
    )
    for row in cur:
        (
            nen,
            tsukihi,
            keibajo,
            kai,
            nichime,
            rbango,
            track,
            kyori,
            shiba,
            dirt,
            j2,
            j3,
        ) = row
        try:
            year = int(nen)
        except (ValueError, TypeError):
            continue
        surface = track_to_surface(track)
        race_info[(nen, tsukihi, keibajo, kai, nichime, rbango)] = {
            "year": year,
            "surface": surface,
            "dband": kyori_to_distance_band(kyori),
            "venue": keibajo,
            "going": resolve_going(surface, shiba, dirt),
            "is_maiden": "701" in (j2, j3),
        }
    logger.info(f"  jvd_race: {len(race_info):,} レース")
    return race_info


# ============================================================
# 単一スキャン: 全 jvd_race_uma を読み、全エンジンへ投入＋新馬戦出走馬を収集
# ============================================================


def build_engines():
    return {
        "sire": AsOfStats(THRESHOLDS["sire"]),
        "nicks": AsOfStats(THRESHOLDS["nicks"]),
        "cond": AsOfStats(THRESHOLDS["condition"]),
        "human": AsOfStats(THRESHOLDS["human"]),
        "draw": AsOfStats(THRESHOLDS["draw"]),
        "weight": AsOfStats(THRESHOLDS["weight"]),
    }


def scan_and_collect(conn, sire_map, a4a5_map, seisansha_map, race_info):
    """jvd_race_uma を1回走査。統計エンジンへ投入し、新馬戦出走馬を年別に収集。"""
    logger.info("jvd_race_uma を走査中（統計投入＋新馬戦収集）...")
    eng = build_engines()
    runners_by_year: dict[int, list[dict]] = {}

    cur = conn.execute(
        "SELECT kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, "
        "  race_bango, umaban, ketto_toroku_bango, kakutei_chakujun, tansho_odds, "
        "  tansho_ninki_jun, ijo_kubun_code, wakuban, futan_juryo, "
        "  kishu_code, chokyoshi_code, banushi_code "
        "FROM jvd_race_uma"
    )
    n = 0
    for row in cur:
        (
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
            ninki,
            ijo,
            waku,
            futan,
            kishu,
            chokyoshi,
            banushi,
        ) = row
        n += 1
        info = race_info.get((nen, tsukihi, keibajo, kai, nichime, rbango))
        if info is None:
            continue
        year = info["year"]
        won = chaku == "01"
        roinum = roi_numerator(won, odds)
        sire_b, bms_b = sire_map.get(ketto, (None, None))

        # --- 統計投入（正常完走のみ。ライブ各バッチの base where と一致） ---
        base_valid = chaku not in (None, "", "00") and (ijo is None or ijo == "0")
        if base_valid:
            surface = info["surface"]
            dband = info["dband"]
            going = info["going"]
            # A1/A2: 父・母父（全レース）
            if sire_b:
                eng["sire"].add(("sire",), sire_b, year, won, roinum)
            if bms_b:
                eng["sire"].add(("bms",), bms_b, year, won, roinum)
            # A3: ニックス（全レース）
            if sire_b and bms_b:
                eng["nicks"].add(None, (sire_b, bms_b), year, won, roinum)
            # B1-B4: 条件別（芝/ダートのみ。父・母父それぞれ）
            if surface:
                for role, bango in (("sire", sire_b), ("bms", bms_b)):
                    if not bango:
                        continue
                    eng["cond"].add(
                        (role, "surface", surface), bango, year, won, roinum
                    )
                    if dband:
                        eng["cond"].add(
                            (role, "distance", dband), bango, year, won, roinum
                        )
                    eng["cond"].add((role, "venue", keibajo), bango, year, won, roinum)
                    if going:
                        eng["cond"].add(
                            (role, "going", going), bango, year, won, roinum
                        )
            # C1-C3: 人的要素（全レース）
            if chokyoshi and chokyoshi.strip():
                eng["human"].add(("trainer",), chokyoshi.strip(), year, won, roinum)
            if kishu and kishu.strip():
                eng["human"].add(("jockey",), kishu.strip(), year, won, roinum)
            if banushi and banushi.strip():
                eng["human"].add(("owner",), banushi.strip(), year, won, roinum)
            seisansha = seisansha_map.get(ketto)
            if seisansha:
                eng["human"].add(("breeder",), seisansha, year, won, roinum)
            # E1/E2: 枠順・斤量（新馬戦701のみ。calc_condition_stats と一致）
            if info["is_maiden"]:
                if surface and waku and waku.strip():
                    eng["draw"].add(
                        (keibajo, surface, dband), waku.strip(), year, won, roinum
                    )
                if futan and futan.strip():
                    eng["weight"].add(None, futan.strip(), year, won, roinum)

        # --- 新馬戦出走馬の収集（スコア対象。出走の有無のみ、完走可否は問わない） ---
        if (
            info["is_maiden"]
            and CLEAN_START <= year <= CLEAN_END
            and umaban
            and umaban.strip()
        ):
            try:
                odds_real = int(odds) / 10.0 if odds and odds.strip() else None
            except (ValueError, TypeError):
                odds_real = None
            try:
                ninki_val = int(ninki) if ninki and ninki.strip() else None
            except (ValueError, TypeError):
                ninki_val = None
            coi_val, outbreed = a4a5_map.get(ketto, (None, None))
            runners_by_year.setdefault(year, []).append(
                {
                    "race_id": f"{nen}{tsukihi}{keibajo}{kai}{nichime}{rbango}",
                    "kaisai_nen": nen,
                    "umaban": umaban.strip(),
                    "ketto": ketto,
                    "sire_b": sire_b,
                    "bms_b": bms_b,
                    "trainer": (chokyoshi or "").strip() or None,
                    "jockey": (kishu or "").strip() or None,
                    "owner": (banushi or "").strip() or None,
                    "breeder": seisansha_map.get(ketto),
                    "waku": (waku or "").strip() or None,
                    "futan": (futan or "").strip() or None,
                    "surface": info["surface"],
                    "dband": info["dband"],
                    "venue": keibajo,
                    "going": info["going"],
                    "won": 1 if won else 0,
                    "odds": odds_real,
                    "ninki": ninki_val,
                    "chaku": chaku,
                    "coi": coi_val,
                    "outbreed": outbreed,
                }
            )

    total_runners = sum(len(v) for v in runners_by_year.values())
    logger.info(f"  走査完了: {n:,} 行 / 新馬戦出走馬 {total_runners:,} 頭")
    return eng, runners_by_year


# ============================================================
# 年次スコアリング
# ============================================================


def score_runner(r: dict, snaps: dict) -> dict:
    """1頭分の as-of 特徴量（pctl）を算出。データ無しは None。"""
    f = dict.fromkeys(FEAT_COLS)
    f["a1_wr"], f["a1_roi"] = feature(snaps["sire"], ("sire",), r["sire_b"])
    f["a2_wr"], f["a2_roi"] = feature(snaps["sire"], ("bms",), r["bms_b"])
    if r["sire_b"] and r["bms_b"]:
        f["a3_wr"], f["a3_roi"] = feature(
            snaps["nicks"], None, (r["sire_b"], r["bms_b"])
        )

    cond = snaps["cond"]
    for sub, ctype, cval in (
        ("b1", "surface", r["surface"]),
        ("b2", "distance", r["dband"]),
        ("b3", "venue", r["venue"]),
        ("b4", "going", r["going"]),
    ):
        if not cval:
            continue
        f[f"{sub}_sire_wr"], f[f"{sub}_sire_roi"] = feature(
            cond, ("sire", ctype, cval), r["sire_b"]
        )
        f[f"{sub}_bms_wr"], f[f"{sub}_bms_roi"] = feature(
            cond, ("bms", ctype, cval), r["bms_b"]
        )

    f["c1_wr"], f["c1_roi"] = feature(snaps["human"], ("trainer",), r["trainer"])
    f["c2_wr"], f["c2_roi"] = feature(snaps["human"], ("jockey",), r["jockey"])
    f["c3_owner_wr"], f["c3_owner_roi"] = feature(
        snaps["human"], ("owner",), r["owner"]
    )
    f["c3_breeder_wr"], f["c3_breeder_roi"] = feature(
        snaps["human"], ("breeder",), r["breeder"]
    )

    if r["surface"] and r["dband"]:
        f["e1_wr"], f["e1_roi"] = feature(
            snaps["draw"], (r["venue"], r["surface"], r["dband"]), r["waku"]
        )
    f["e2_wr"], f["e2_roi"] = feature(snaps["weight"], None, r["futan"])
    return f


def score_all_years(eng: dict, runners_by_year: dict) -> list[tuple]:
    """年昇順に各エンジンを advance→snapshot し、全出走馬の特徴量行を構築。"""
    rows: list[tuple] = []
    for year in range(CLEAN_START, CLEAN_END + 1):
        runners = runners_by_year.get(year)
        if not runners:
            continue
        for e in eng.values():
            e.advance_to(year)
        snaps = {k: e.snapshot() for k, e in eng.items()}
        logger.info(f"  {year}: 出走馬 {len(runners):,} 頭をスコア化")
        for r in runners:
            f = score_runner(r, snaps)
            rows.append(
                (
                    r["race_id"],
                    r["kaisai_nen"],
                    r["umaban"],
                    r["ketto"],
                    year,
                    r["won"],
                    r["odds"],
                    r["ninki"],
                    r["chaku"],
                    r["coi"],
                    r["outbreed"],
                    *(f[c] for c in FEAT_COLS),
                )
            )
    return rows


# ============================================================
# テーブル作成・書き込み
# ============================================================


def create_cache_table(conn: sqlite3.Connection) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {CACHE_TABLE}")
    feat_defs = ",\n            ".join(f"{c} REAL" for c in FEAT_COLS)
    conn.execute(f"""
        CREATE TABLE {CACHE_TABLE} (
            race_id          TEXT NOT NULL,
            kaisai_nen       TEXT NOT NULL,
            umaban           TEXT NOT NULL,
            ketto_toroku_bango TEXT,
            as_of_year       INTEGER NOT NULL,
            won              INTEGER,
            tansho_odds      REAL,
            ninki            INTEGER,
            chakujun         TEXT,
            a4_coi           REAL,
            a5_outbreed      INTEGER,
            {feat_defs},
            updated_at       TEXT,
            PRIMARY KEY (race_id, umaban)
        )
    """)
    conn.execute(f"CREATE INDEX idx_{CACHE_TABLE}_year ON {CACHE_TABLE} (as_of_year)")
    conn.commit()


def write_rows(conn: sqlite3.Connection, rows: list[tuple], now: str) -> None:
    meta = [
        "race_id",
        "kaisai_nen",
        "umaban",
        "ketto_toroku_bango",
        "as_of_year",
        "won",
        "tansho_odds",
        "ninki",
        "chakujun",
        "a4_coi",
        "a5_outbreed",
    ]
    cols = meta + FEAT_COLS + ["updated_at"]
    placeholders = ", ".join("?" for _ in cols)
    rows_with_ts = [r + (now,) for r in rows]
    conn.executemany(
        f"INSERT OR REPLACE INTO {CACHE_TABLE} ({', '.join(cols)}) VALUES ({placeholders})",
        rows_with_ts,
    )
    conn.commit()


# ============================================================
# メイン
# ============================================================


def main() -> None:
    global CLEAN_START, CLEAN_END
    ap = argparse.ArgumentParser(description="as-of サブスコア前計算（#B1）")
    ap.add_argument("--start-year", type=int, default=CLEAN_START)
    ap.add_argument("--end-year", type=int, default=CLEAN_END)
    args = ap.parse_args()
    CLEAN_START, CLEAN_END = args.start_year, args.end_year

    logger.info(f"DB: {DB_PATH}")
    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    start = time.time()

    try:
        sire_map, a4a5_map, seisansha_map = load_pedigree_maps(conn)
        race_info = load_race_info(conn)
        eng, runners_by_year = scan_and_collect(
            conn, sire_map, a4a5_map, seisansha_map, race_info
        )
        # メモリ節約: 走査後はマスタマップを解放
        del sire_map, a4a5_map, seisansha_map, race_info
        rows = score_all_years(eng, runners_by_year)

        now = datetime.now(timezone.utc).isoformat()
        create_cache_table(conn)
        write_rows(conn, rows, now)

        # サマリー
        summary = conn.execute(
            f"SELECT COUNT(*), SUM(won), "
            f"  SUM(CASE WHEN a1_wr IS NOT NULL THEN 1 ELSE 0 END), "
            f"  SUM(CASE WHEN c2_wr IS NOT NULL THEN 1 ELSE 0 END) "
            f"FROM {CACHE_TABLE}"
        ).fetchone()
        logger.info("=== 前計算サマリー ===")
        logger.info(f"  出走馬: {summary[0]:,} 頭 / 勝利 {summary[1]:,}")
        logger.info(f"  A1(父)あり: {summary[2]:,} / C2(騎手)あり: {summary[3]:,}")
    finally:
        conn.close()

    logger.info(f"完了 ({time.time() - start:.1f}秒)")


if __name__ == "__main__":
    main()

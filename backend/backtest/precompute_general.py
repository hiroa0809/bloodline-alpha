"""Direction A: 一般戦の as-of サブスコア前計算（ファンダ＋スピード統合）。

新馬戦専用の precompute_subscores.py を「一般戦（非新馬・平地戦）」へ土俵移しした版。
ファンダ（血統A・条件B・人的C・斤量E）の as-of パーセンタイルに加え、過去走タイムから
作る **スピード as-of 特徴** を同じ出走馬行へ統合し、backtest_subscore_cache_general へ格納する。

なぜ別ファイルか:
  precompute_subscores.py は新馬戦701で本番確定済みで top1_core 等が依存する。既存挙動を
  壊さないため一般戦版は新規ファイルとし、共通部品（統計エンジン・血統/レースマップ・
  ファンダ score_runner・スピード as-of 関数）は import で流用する（surgical changes）。

新馬戦版との違いは3点だけ:
  1. スコア出力対象を is_maiden(701) → 「非新馬・平地戦」に差し替え。
  2. E1/E2（枠順・斤量）の母集団を新馬戦701 → 一般戦（非新馬）に変更。
  3. スピード as-of 特徴（走破 best/recent/avg・上り3F best）を生値＋as-of パーセンタイルで追加。
     生値はトラックA（③信号診断＝順序のみ使うAUC）用、pctl はトラックB（束ねスコア）用。

金庫ルール:
  本バッチはキャッシュ生成であり評価ではない。新馬戦版と同様に全クリーン期間
  （1986-2025）を前計算する（OOS含む）。OOS封印は読み手側（analyze_*_general は
  SQL の年範囲で IS に限定、optimize/run_backtest は IS スライス）で担保する。
  point-in-time: 統計は対象年より前の年のみ、過去走集約は対象レース日より厳密に前。

使い方:
    python backend/backtest/precompute_general.py
    python backend/backtest/precompute_general.py --start-year 2008 --end-year 2010  # 部分検証
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from backtest.analyze_speed_signal import (  # noqa: E402
    _safe_chaku,
    assign_horse_features,
    assign_speed_values,
    parse_ato3f,
    parse_soha,
)
from backtest.asof_helpers import (  # noqa: E402
    kyori_to_distance_band,
    percentile_rank,
    resolve_going,
    roi_numerator,
    track_to_surface,
)
from backtest.precompute_subscores import (  # noqa: E402
    FEAT_COLS,
    build_engines,
    load_pedigree_maps,
    score_runner,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

DB_PATH = _BACKEND_DIR / "bloodline.db"

# クリーン期間（CLAUDE.md バックテスト方法論）。precompute は全期間生成（封印は読み手側）。
CLEAN_START = 1986
CLEAN_END = 2025

CACHE_TABLE = "backtest_subscore_cache_general"

# スピード列: 生値（トラックA・AUCは順序のみ）＋ as-of パーセンタイル（トラックB・束ね用）。
SPEED_RAW_COLS = ["sp_soha_best", "sp_soha_recent", "sp_soha_avg", "sp_ato3f_best"]
SPEED_FKEYS = ["f_best", "f_recent", "f_avg", "f_ato_best"]  # runs 上の集約キーと対応
SPEED_PCTL_COLS = [c + "_pctl" for c in SPEED_RAW_COLS]
ALL_SPEED_COLS = SPEED_RAW_COLS + SPEED_PCTL_COLS


# ============================================================
# レース情報（kyori 生＝スピードkey用 と dband＝ファンダ用 を両方持つ）
# ============================================================


def load_race_info_general(conn: sqlite3.Connection) -> dict:
    """jvd_race → レースPK → 条件情報。スピード(kyori生)とファンダ(dband)双方を保持。"""
    logger.info("レース条件マップを構築中（一般戦・スピード両用）...")
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
        surface = track_to_surface(track)  # 障害は None
        race_info[(nen, tsukihi, keibajo, kai, nichime, rbango)] = {
            "year": year,
            "surface": surface,
            "kyori": (kyori or "").strip() or None,
            "dband": kyori_to_distance_band(kyori),
            "venue": keibajo,
            "going": resolve_going(surface, shiba, dirt),
            "is_maiden": "701" in (j2, j3),
        }
    logger.info(f"  jvd_race: {len(race_info):,} レース")
    return race_info


# ============================================================
# 単一スキャン: ファンダ統計投入 ＋ 平地出走の収集（スピード＋ファンダ素データ）
# ============================================================


def scan_and_collect(conn, sire_map, a4a5_map, seisansha_map, race_info):
    """jvd_race_uma を1回走査。統計エンジンへ投入し、平地戦の出走行を runs に収集する。

    runs は新馬を含む全平地出走（スピード標準・過去走集約の母集団に必要）。スコア対象
    （非新馬）の抽出は後段で行う。E1/E2 の母集団は一般戦（非新馬）に限定する。
    """
    logger.info("jvd_race_uma を走査中（統計投入＋平地戦収集）...")
    eng = build_engines()
    runs: list[dict] = []

    cur = conn.execute(
        "SELECT kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, "
        "  race_bango, umaban, ketto_toroku_bango, kakutei_chakujun, tansho_odds, "
        "  tansho_ninki_jun, ijo_kubun_code, wakuban, futan_juryo, "
        "  kishu_code, chokyoshi_code, banushi_code, soha_time, ato_3f_time "
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
            soha,
            ato3f,
        ) = row
        n += 1
        info = race_info.get((nen, tsukihi, keibajo, kai, nichime, rbango))
        if info is None:
            continue
        year = info["year"]
        won = chaku == "01"
        roinum = roi_numerator(won, odds)
        sire_b, bms_b = sire_map.get(ketto, (None, None))
        surface = info["surface"]
        dband = info["dband"]
        going = info["going"]
        is_maiden = info["is_maiden"]

        # --- 統計投入（正常完走のみ。ライブ各バッチの base where と一致） ---
        base_valid = chaku not in (None, "", "00") and (ijo is None or ijo == "0")
        if base_valid:
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
            # E1/E2: 枠順・斤量（一般戦＝非新馬のみ。土俵移しに伴い母集団を非新馬に変更）
            if not is_maiden:
                if surface and waku and waku.strip():
                    eng["draw"].add(
                        (keibajo, surface, dband), waku.strip(), year, won, roinum
                    )
                if futan and futan.strip():
                    eng["weight"].add(None, futan.strip(), year, won, roinum)

        # --- 平地出走の収集（新馬含む。スピード標準・集約の母集団に必要） ---
        if surface is None or not (umaban and umaban.strip()):
            continue  # 障害・馬番欠損は対象外
        try:
            odds_real = int(odds) / 10.0 if odds and odds.strip() else None
        except (ValueError, TypeError):
            odds_real = None
        try:
            ninki_val = int(ninki) if ninki and ninki.strip() else None
        except (ValueError, TypeError):
            ninki_val = None
        chaku_i = _safe_chaku(chaku)
        valid_finish = chaku_i >= 1 and (ijo is None or ijo == "0")
        coi_val, outbreed = a4a5_map.get(ketto, (None, None))
        runs.append(
            {
                "date": f"{nen}{tsukihi}",
                "year": year,
                "race_id": f"{nen}{tsukihi}{keibajo}{kai}{nichime}{rbango}",
                "kaisai_nen": nen,
                "umaban": umaban.strip(),
                "ketto": ketto,
                "chaku": chaku,
                "odds": odds_real,
                "ninki": ninki_val,
                "won": 1 if won else 0,
                "is_maiden": is_maiden,
                "key": (keibajo, surface, info["kyori"]),  # スピード標準のキー
                "soha": parse_soha(soha) if valid_finish else None,
                "ato3f": parse_ato3f(ato3f) if valid_finish else None,
                # ファンダ score_runner 用素データ
                "sire_b": sire_b,
                "bms_b": bms_b,
                "trainer": (chokyoshi or "").strip() or None,
                "jockey": (kishu or "").strip() or None,
                "owner": (banushi or "").strip() or None,
                "breeder": seisansha_map.get(ketto),
                "waku": (waku or "").strip() or None,
                "futan": (futan or "").strip() or None,
                "surface": surface,
                "dband": dband,
                "venue": keibajo,
                "going": going,
                "coi": coi_val,
                "outbreed": outbreed,
            }
        )

    logger.info(f"  走査完了: {n:,} 行 / 平地戦 出走行 {len(runs):,}")
    return eng, runs


# ============================================================
# 年次スコアリング（ファンダ as-of pctl ＋ スピード生値/as-of pctl）
# ============================================================


def score_all_years(eng: dict, runs: list[dict]) -> list[tuple]:
    """非新馬の各出走馬へファンダ as-of pctl とスピード生値/as-of pctl を付与し行を構築。

    スピード pctl の母集団は『対象年より前の非新馬・平地出走馬の同集約値』（年次累積）。
    生値は欠損(過去走ゼロ=nan)なら None、pctl も母集団が空 or 値欠損なら None。
    """
    # 対象（非新馬・平地）を年別にグループ化。runs には集約値 f_* が付与済み。
    targets_by_year: dict[int, list[dict]] = {}
    for r in runs:
        if not r["is_maiden"] and CLEAN_START <= r["year"] <= CLEAN_END:
            targets_by_year.setdefault(r["year"], []).append(r)

    def _fval(r: dict, fkey: str):
        v = r.get(fkey)
        return None if v is None or v != v else float(v)  # v!=v は NaN 判定

    rows: list[tuple] = []
    prior: dict[str, list[float]] = {c: [] for c in SPEED_RAW_COLS}  # year未満の累積
    for year in range(CLEAN_START, CLEAN_END + 1):
        runners = targets_by_year.get(year)
        if not runners:
            continue
        for e in eng.values():
            e.advance_to(year)
        snaps = {k: e.snapshot() for k, e in eng.items()}
        sorted_prior = {c: sorted(prior[c]) for c in SPEED_RAW_COLS}
        logger.info(f"  {year}: 出走馬 {len(runners):,} 頭をスコア化（非新馬・平地）")

        for r in runners:
            f = score_runner(r, snaps)  # ファンダ pctl（新馬戦版と同一ロジック）
            sp_raw: list[float | None] = []
            sp_pctl: list[float | None] = []
            for raw_col, fkey in zip(SPEED_RAW_COLS, SPEED_FKEYS):
                v = _fval(r, fkey)
                sp_raw.append(v)
                if v is None or not sorted_prior[raw_col]:
                    sp_pctl.append(None)
                else:
                    sp_pctl.append(percentile_rank(sorted_prior[raw_col], v))
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
                    *sp_raw,
                    *sp_pctl,
                )
            )

        # この年の集約値を母集団へ追加（次年以降の pctl 母集団＝strictly-prior を担保）。
        for r in runners:
            for raw_col, fkey in zip(SPEED_RAW_COLS, SPEED_FKEYS):
                v = _fval(r, fkey)
                if v is not None:
                    prior[raw_col].append(v)
    return rows


# ============================================================
# テーブル作成・書き込み
# ============================================================


def create_cache_table(conn: sqlite3.Connection) -> None:
    """backtest_subscore_cache_general を再作成（ファンダ＋スピード列）。"""
    conn.execute(f"DROP TABLE IF EXISTS {CACHE_TABLE}")
    feat_defs = ",\n            ".join(f"{c} REAL" for c in FEAT_COLS + ALL_SPEED_COLS)
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
    """前計算した全出走馬の行を一括書き込みする。"""
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
    cols = meta + FEAT_COLS + ALL_SPEED_COLS + ["updated_at"]
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
    """マップ構築→全件走査→スピード付与→年次スコアリング→キャッシュ書き込み。"""
    global CLEAN_START, CLEAN_END
    ap = argparse.ArgumentParser(
        description="一般戦 as-of サブスコア前計算（Direction A）"
    )
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
        race_info = load_race_info_general(conn)
        eng, runs = scan_and_collect(conn, sire_map, a4a5_map, seisansha_map, race_info)
        del sire_map, a4a5_map, race_info  # メモリ節約（seisansha は runs に展開済み）

        # スピード as-of: 標準タイム→スピード値→過去走集約（analyze_speed_signal と同一）。
        logger.info("スピード as-of 特徴を構築中（標準タイム→過去走集約）...")
        assign_speed_values(runs)
        assign_horse_features(
            runs
        )  # runs を date 昇順に並べ f_best/recent/avg/ato_best 付与

        rows = score_all_years(eng, runs)

        now = datetime.now(timezone.utc).isoformat()
        create_cache_table(conn)
        write_rows(conn, rows, now)

        summary = conn.execute(
            f"SELECT COUNT(*), SUM(won), "
            f"  SUM(CASE WHEN c2_wr IS NOT NULL THEN 1 ELSE 0 END), "
            f"  SUM(CASE WHEN sp_soha_best IS NOT NULL THEN 1 ELSE 0 END) "
            f"FROM {CACHE_TABLE}"
        ).fetchone()
        logger.info("=== 前計算サマリー（一般戦） ===")
        logger.info(f"  出走馬: {summary[0]:,} 頭 / 勝利 {summary[1]:,}")
        logger.info(
            f"  C2(騎手)あり: {summary[2]:,} / 走破best(過去走あり): {summary[3]:,}"
        )
    finally:
        conn.close()

    logger.info(f"完了 ({time.time() - start:.1f}秒)")


if __name__ == "__main__":
    main()

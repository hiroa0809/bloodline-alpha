"""
種牡馬・母父(BMS)別 条件別成績集計バッチ

jvd_race_uma × jvd_race をJOINし、レース条件（馬場・距離帯・競馬場・馬場状態）ごとに
父・母父の成績を集計して sire_condition_stats テーブルに格納する。

スコアリングエンジン カテゴリB（B1-馬場, B2-距離, B3-開催地, B4-馬場状態）の土台データ。

使い方:
    python backend/batch/calc_sire_condition_stats.py
"""

import ast
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "bloodline.db"


# --- 条件値の定義 ---

# B1: 馬場 — track_code先頭1桁で判定
SURFACE_CASE = """
    CASE WHEN SUBSTR(r.track_code, 1, 1) = '1' THEN 'turf'
         WHEN SUBSTR(r.track_code, 1, 1) = '2' THEN 'dirt'
    END
"""

# B2: 距離帯 — 4区分にグルーピング
DISTANCE_CASE = """
    CASE WHEN CAST(r.kyori AS INTEGER) <= 1400 THEN 'sprint'
         WHEN CAST(r.kyori AS INTEGER) <= 1800 THEN 'mile'
         WHEN CAST(r.kyori AS INTEGER) <= 2200 THEN 'middle'
         ELSE 'long'
    END
"""

# B4: 馬場状態 — 馬場種別×馬場状態の組合せ
GOING_CASE = """
    CASE
        WHEN SUBSTR(r.track_code, 1, 1) = '1' AND r.shiba_baba_jotai_code = '1' THEN 'turf_good'
        WHEN SUBSTR(r.track_code, 1, 1) = '1' AND r.shiba_baba_jotai_code = '2' THEN 'turf_yielding'
        WHEN SUBSTR(r.track_code, 1, 1) = '1' AND r.shiba_baba_jotai_code = '3' THEN 'turf_soft'
        WHEN SUBSTR(r.track_code, 1, 1) = '1' AND r.shiba_baba_jotai_code = '4' THEN 'turf_heavy'
        WHEN SUBSTR(r.track_code, 1, 1) = '2' AND r.dirt_baba_jotai_code = '1' THEN 'dirt_good'
        WHEN SUBSTR(r.track_code, 1, 1) = '2' AND r.dirt_baba_jotai_code = '2' THEN 'dirt_yielding'
        WHEN SUBSTR(r.track_code, 1, 1) = '2' AND r.dirt_baba_jotai_code = '3' THEN 'dirt_soft'
        WHEN SUBSTR(r.track_code, 1, 1) = '2' AND r.dirt_baba_jotai_code = '4' THEN 'dirt_heavy'
    END
"""

# JOINの共通WHERE句
COMMON_WHERE = """
    WHERE m.{bango_col} IS NOT NULL
      AND m.{bango_col} != ''
      AND ru.kakutei_chakujun IS NOT NULL
      AND ru.kakutei_chakujun != ''
      AND ru.kakutei_chakujun != '00'
      AND (ru.ijo_kubun_code IS NULL OR ru.ijo_kubun_code = '0')
      AND SUBSTR(r.track_code, 1, 1) IN ('1', '2')
"""

# jvd_race_uma × jvd_race のJOIN（6カラム複合PK）
RACE_JOIN = """
    FROM _tmp_uma_sire m
    JOIN jvd_race_uma ru ON m.ketto_toroku_bango = ru.ketto_toroku_bango
    JOIN jvd_race r ON ru.kaisai_nen = r.kaisai_nen
        AND ru.kaisai_tsukihi = r.kaisai_tsukihi
        AND ru.keibajo_code = r.keibajo_code
        AND ru.kaisai_kai = r.kaisai_kai
        AND ru.kaisai_nichime = r.kaisai_nichime
        AND ru.race_bango = r.race_bango
"""

# 集計カラム（SELECT句の共通部分）
AGG_COLUMNS = """
    COUNT(*) AS starts,
    SUM(CASE WHEN ru.kakutei_chakujun = '01' THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN ru.kakutei_chakujun = '02' THEN 1 ELSE 0 END) AS second_place,
    SUM(CASE WHEN ru.kakutei_chakujun = '03' THEN 1 ELSE 0 END) AS third_place,
    CAST(SUM(CASE WHEN ru.kakutei_chakujun = '01' THEN 1 ELSE 0 END) AS REAL)
        / COUNT(*) AS win_rate,
    CAST(SUM(CASE WHEN ru.kakutei_chakujun IN ('01','02') THEN 1 ELSE 0 END) AS REAL)
        / COUNT(*) AS rentai_rate,
    CAST(SUM(CASE WHEN ru.kakutei_chakujun IN ('01','02','03') THEN 1 ELSE 0 END) AS REAL)
        / COUNT(*) AS fukusho_rate,
    CAST(
        SUM(CASE
            WHEN ru.kakutei_chakujun = '01' AND ru.tansho_odds IS NOT NULL
                AND ru.tansho_odds != '' AND CAST(ru.tansho_odds AS INTEGER) > 0
            THEN CAST(ru.tansho_odds AS INTEGER) * 10.0
            ELSE 0
        END)
        / (COUNT(*) * 100.0)
    AS REAL) AS tansho_roi
"""


def create_table(conn: sqlite3.Connection) -> None:
    """sire_condition_stats テーブルを再作成"""
    conn.execute("DROP TABLE IF EXISTS sire_condition_stats")
    conn.execute("""
        CREATE TABLE sire_condition_stats (
            hanshoku_bango  TEXT NOT NULL,
            role            TEXT NOT NULL,
            condition_type  TEXT NOT NULL,
            condition_value TEXT NOT NULL,
            bamei           TEXT,
            starts          INTEGER DEFAULT 0,
            wins            INTEGER DEFAULT 0,
            second          INTEGER DEFAULT 0,
            third           INTEGER DEFAULT 0,
            win_rate        REAL DEFAULT 0,
            rentai_rate     REAL DEFAULT 0,
            fukusho_rate    REAL DEFAULT 0,
            tansho_roi      REAL DEFAULT 0,
            updated_at      TEXT,
            PRIMARY KEY (hanshoku_bango, role, condition_type, condition_value)
        )
    """)
    conn.commit()


def build_sire_mapping(conn: sqlite3.Connection) -> int:
    """jvd_uma.sandai_ketto を解析して一時マッピングテーブルを構築"""
    conn.execute("DROP TABLE IF EXISTS _tmp_uma_sire")
    conn.execute("""
        CREATE TABLE _tmp_uma_sire (
            ketto_toroku_bango TEXT PRIMARY KEY,
            sire_bango         TEXT,
            sire_bamei         TEXT,
            bms_bango          TEXT,
            bms_bamei          TEXT
        )
    """)
    conn.commit()

    logger.info("父・母父マッピングを構築中...")
    cursor = conn.execute(
        "SELECT ketto_toroku_bango, sandai_ketto FROM jvd_uma WHERE sandai_ketto IS NOT NULL"
    )

    batch = []
    count = 0
    errors = 0

    for row in cursor:
        ketto_bango, sandai_raw = row
        try:
            ketto_list = ast.literal_eval(sandai_raw)
        except (ValueError, SyntaxError):
            errors += 1
            continue

        sire_bango = None
        sire_bamei = None
        bms_bango = None
        bms_bamei = None

        if len(ketto_list) > 0 and isinstance(ketto_list[0], dict):
            sire_bango = ketto_list[0].get("hanshoku_toroku_bango")
            sire_bamei = ketto_list[0].get("bamei")
        if len(ketto_list) > 4 and isinstance(ketto_list[4], dict):
            bms_bango = ketto_list[4].get("hanshoku_toroku_bango")
            bms_bamei = ketto_list[4].get("bamei")

        if sire_bango and sire_bango.strip():
            batch.append((ketto_bango, sire_bango.strip(), sire_bamei, bms_bango, bms_bamei))
            count += 1

        if len(batch) >= 10000:
            conn.executemany(
                "INSERT OR IGNORE INTO _tmp_uma_sire VALUES (?, ?, ?, ?, ?)", batch
            )
            conn.commit()
            batch.clear()

    if batch:
        conn.executemany(
            "INSERT OR IGNORE INTO _tmp_uma_sire VALUES (?, ?, ?, ?, ?)", batch
        )
        conn.commit()

    logger.info(f"マッピング完了: {count:,} 件（解析エラー: {errors} 件）")
    return count


# --- 集計SQL構築 ---

# condition_type ごとの設定: (condition_value式, 追加WHERE条件)
_CONDITION_DEFS = {
    "surface": (SURFACE_CASE, ""),
    "distance": (DISTANCE_CASE, ""),
    "venue": ("r.keibajo_code", ""),
    "going": (GOING_CASE, "AND ({case_expr}) IS NOT NULL"),
}


def _build_condition_aggregate_sql(
    bango_col: str,
    bamei_col: str,
    role: str,
    condition_type: str,
    now: str,
) -> str:
    """条件別集計クエリを構築"""
    case_expr, extra_where = _CONDITION_DEFS[condition_type]
    # going の場合、CASE式がNULL（馬場状態コード'0'）の行を除外
    if extra_where:
        extra_where = extra_where.format(case_expr=case_expr)
    where_clause = COMMON_WHERE.format(bango_col=bango_col) + "\n      " + extra_where

    return f"""
        INSERT INTO sire_condition_stats
            (hanshoku_bango, role, condition_type, condition_value, bamei,
             starts, wins, second, third, win_rate, rentai_rate, fukusho_rate, tansho_roi, updated_at)
        SELECT
            m.{bango_col},
            '{role}',
            '{condition_type}',
            {case_expr} AS condition_value,
            m.{bamei_col},
            {AGG_COLUMNS},
            '{now}'
        {RACE_JOIN}
        {where_clause}
        GROUP BY m.{bango_col}, condition_value
        HAVING condition_value IS NOT NULL
    """


# role × condition_type の全パターンを事前定義（SQLインジェクション防止）
_ALL_AGGREGATE_SQLS = {}
for _role, (_bango, _bamei) in [("sire", ("sire_bango", "sire_bamei")), ("bms", ("bms_bango", "bms_bamei"))]:
    for _ctype in _CONDITION_DEFS:
        _ALL_AGGREGATE_SQLS[(_role, _ctype)] = lambda now, b=_bango, bm=_bamei, r=_role, ct=_ctype: \
            _build_condition_aggregate_sql(b, bm, r, ct, now)


def aggregate_condition_stats(conn: sqlite3.Connection, role: str, condition_type: str) -> int:
    """指定ロール×条件タイプの集計を実行"""
    key = (role, condition_type)
    if key not in _ALL_AGGREGATE_SQLS:
        raise ValueError(f"不正な組合せ: {key}")

    logger.info(f"集計中: role={role}, condition_type={condition_type}")
    now = datetime.now(timezone.utc).isoformat()
    sql = _ALL_AGGREGATE_SQLS[key](now)

    cursor = conn.execute(sql)
    conn.commit()
    count = cursor.rowcount
    logger.info(f"  → {count:,} 件")
    return count


def cleanup(conn: sqlite3.Connection) -> None:
    """一時テーブルを削除"""
    conn.execute("DROP TABLE IF EXISTS _tmp_uma_sire")
    conn.commit()


def main() -> None:
    logger.info(f"DB: {DB_PATH}")
    if not DB_PATH.exists():
        logger.error(f"DBファイルが見つかりません: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=120)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    start = time.time()

    try:
        create_table(conn)
        build_sire_mapping(conn)

        # インデックス作成（集計クエリ高速化）
        logger.info("一時テーブルにインデックス作成中...")
        conn.execute("CREATE INDEX IF NOT EXISTS _idx_tmp_sire ON _tmp_uma_sire (sire_bango)")
        conn.execute("CREATE INDEX IF NOT EXISTS _idx_tmp_bms ON _tmp_uma_sire (bms_bango)")
        conn.commit()

        # 8パターン（2ロール × 4条件タイプ）の集計を実行
        total_rows = 0
        for role in ("sire", "bms"):
            for ctype in ("surface", "distance", "venue", "going"):
                total_rows += aggregate_condition_stats(conn, role, ctype)

        # 結果サマリー
        logger.info("=== 集計結果サマリー ===")
        logger.info(f"合計: {total_rows:,} 件")
        rows = conn.execute(
            "SELECT role, condition_type, COUNT(*), SUM(starts), SUM(wins) "
            "FROM sire_condition_stats GROUP BY role, condition_type ORDER BY role, condition_type"
        ).fetchall()
        for r in rows:
            logger.info(f"  {r[0]:4s} / {r[1]:10s}: {r[2]:>6,} 件, 延べ {r[3]:>10,} 走, {r[4]:>7,} 勝")

    finally:
        cleanup(conn)
        conn.close()

    elapsed = time.time() - start
    logger.info(f"完了 ({elapsed:.1f}秒)")


if __name__ == "__main__":
    main()

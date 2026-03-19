"""
父×母父(BMS)ニックス成績集計バッチ

jvd_race_uma × jvd_uma をJOINし、父×BMS の組合せごとに成績を集計して
nicks_stats テーブルに格納する。

スコアリングエンジン カテゴリA（A3:ニックス）の土台データ。

使い方:
    python backend/batch/calc_nicks_stats.py
"""

import ast
import json
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

WORK_TABLE = "nicks_stats_new"


def create_work_table(conn: sqlite3.Connection) -> None:
    """作業用テーブルを作成（集計完了後にアトミックスワップ）"""
    conn.execute(f"DROP TABLE IF EXISTS {WORK_TABLE}")
    conn.execute(f"""
        CREATE TABLE {WORK_TABLE} (
            sire_bango      TEXT NOT NULL,
            bms_bango       TEXT NOT NULL,
            sire_bamei      TEXT,
            bms_bamei       TEXT,
            runners         INTEGER DEFAULT 0,
            starts          INTEGER DEFAULT 0,
            wins            INTEGER DEFAULT 0,
            win_rate        REAL DEFAULT 0,
            tansho_roi      REAL DEFAULT 0,
            updated_at      TEXT,
            PRIMARY KEY (sire_bango, bms_bango)
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
            try:
                ketto_list = json.loads(sandai_raw)
            except (json.JSONDecodeError, TypeError):
                ketto_list = ast.literal_eval(sandai_raw)
            if not isinstance(ketto_list, list):
                errors += 1
                continue
        except (ValueError, SyntaxError, TypeError, RecursionError, MemoryError):
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

        sire_bango_norm = (
            sire_bango.strip() if isinstance(sire_bango, str) and sire_bango.strip() else None
        )
        bms_bango_norm = (
            bms_bango.strip() if isinstance(bms_bango, str) and bms_bango.strip() else None
        )

        if sire_bango_norm and bms_bango_norm:
            batch.append((ketto_bango, sire_bango_norm, sire_bamei, bms_bango_norm, bms_bamei))
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


def aggregate_nicks_stats(conn: sqlite3.Connection) -> int:
    """父×BMS の組合せ別成績を集計"""
    logger.info("父×BMS ニックス集計中...")
    now = datetime.now(timezone.utc).isoformat()

    sql = f"""
        INSERT INTO {WORK_TABLE}
            (sire_bango, bms_bango, sire_bamei, bms_bamei,
             runners, starts, wins, win_rate, tansho_roi, updated_at)
        SELECT
            m.sire_bango,
            m.bms_bango,
            m.sire_bamei,
            m.bms_bamei,
            COUNT(DISTINCT ru.ketto_toroku_bango) AS runners,
            COUNT(*) AS starts,
            SUM(CASE WHEN ru.kakutei_chakujun = '01' THEN 1 ELSE 0 END) AS wins,
            CAST(SUM(CASE WHEN ru.kakutei_chakujun = '01' THEN 1 ELSE 0 END) AS REAL)
                / COUNT(*) AS win_rate,
            CAST(
                SUM(CASE
                    WHEN ru.kakutei_chakujun = '01' AND ru.tansho_odds IS NOT NULL
                        AND ru.tansho_odds != '' AND CAST(ru.tansho_odds AS INTEGER) > 0
                    THEN CAST(ru.tansho_odds AS INTEGER) * 10.0
                    ELSE 0
                END)
                / (COUNT(*) * 100.0)
            AS REAL) AS tansho_roi,
            '{now}'
        FROM _tmp_uma_sire m
        JOIN jvd_race_uma ru ON m.ketto_toroku_bango = ru.ketto_toroku_bango
        WHERE m.sire_bango IS NOT NULL AND m.sire_bango != ''
          AND m.bms_bango IS NOT NULL AND m.bms_bango != ''
          AND ru.kakutei_chakujun IS NOT NULL
          AND ru.kakutei_chakujun != ''
          AND ru.kakutei_chakujun != '00'
          AND (ru.ijo_kubun_code IS NULL OR ru.ijo_kubun_code = '0')
        GROUP BY m.sire_bango, m.bms_bango
    """

    cursor = conn.execute(sql)
    conn.commit()
    count = cursor.rowcount
    logger.info(f"ニックス集計完了: {count:,} 組合せ")
    return count


def swap_table(conn: sqlite3.Connection) -> None:
    """作業テーブルを本番テーブルにアトミックスワップ"""
    logger.info("テーブルをアトミックスワップ中...")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS nicks_stats")
        conn.execute(f"ALTER TABLE {WORK_TABLE} RENAME TO nicks_stats")
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    logger.info("スワップ完了")


def cleanup(conn: sqlite3.Connection) -> None:
    """一時テーブルを削除"""
    conn.execute("DROP TABLE IF EXISTS _tmp_uma_sire")
    conn.execute(f"DROP TABLE IF EXISTS {WORK_TABLE}")
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
        create_work_table(conn)
        build_sire_mapping(conn)

        # インデックス作成（集計クエリ高速化）
        logger.info("一時テーブルにインデックス作成中...")
        conn.execute("CREATE INDEX IF NOT EXISTS _idx_tmp_sire ON _tmp_uma_sire (sire_bango)")
        conn.execute("CREATE INDEX IF NOT EXISTS _idx_tmp_bms ON _tmp_uma_sire (bms_bango)")
        conn.commit()

        total = aggregate_nicks_stats(conn)

        # 結果サマリー
        logger.info("=== 集計結果サマリー ===")
        logger.info(f"合計: {total:,} 組合せ")
        summary = conn.execute(f"""
            SELECT COUNT(*), SUM(starts), SUM(wins),
                   ROUND(AVG(win_rate), 4), ROUND(AVG(tansho_roi), 4)
            FROM {WORK_TABLE}
            WHERE starts >= 3
        """).fetchone()
        logger.info(
            f"  starts>=3: {summary[0]:,} 組合せ, "
            f"延べ {summary[1]:,} 走, {summary[2]:,} 勝, "
            f"平均勝率 {summary[3]}, 平均ROI {summary[4]}"
        )

        # アトミックスワップ（全集計成功後に入れ替え）
        swap_table(conn)

    finally:
        cleanup(conn)
        conn.close()

    elapsed = time.time() - start
    logger.info(f"完了 ({elapsed:.1f}秒)")


if __name__ == "__main__":
    main()

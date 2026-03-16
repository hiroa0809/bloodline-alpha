"""
種牡馬・母父(BMS)別成績集計バッチ

jvd_uma.sandai_ketto から父・母父の繁殖登録番号を抽出し、
jvd_race_uma の出走成績と結合して sire_stats テーブルに集計結果を格納する。

使い方:
    python -m backend.batch.calc_sire_stats
    python backend/batch/calc_sire_stats.py
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


def create_tables(conn: sqlite3.Connection) -> None:
    """sire_stats テーブルと一時テーブルを作成"""
    # 毎回再作成（全データ再集計のため）
    conn.execute("DROP TABLE IF EXISTS sire_stats")
    conn.execute("""
        CREATE TABLE sire_stats (
            hanshoku_bango  TEXT NOT NULL,
            role            TEXT NOT NULL,
            bamei           TEXT,
            runners         INTEGER DEFAULT 0,
            starts          INTEGER DEFAULT 0,
            wins            INTEGER DEFAULT 0,
            second          INTEGER DEFAULT 0,
            third           INTEGER DEFAULT 0,
            win_rate        REAL DEFAULT 0,
            rentai_rate     REAL DEFAULT 0,
            fukusho_rate    REAL DEFAULT 0,
            tansho_roi      REAL DEFAULT 0,
            updated_at      TEXT,
            PRIMARY KEY (hanshoku_bango, role)
        )
    """)
    # 馬 → 父/母父 のマッピング一時テーブル
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


def build_sire_mapping(conn: sqlite3.Connection) -> int:
    """jvd_uma.sandai_ketto を解析して一時マッピングテーブルを構築"""
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

        # sandai_ketto: [父, 母, 父父, 父母, 母父, 母母, ...]（14頭）
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

        # 空文字列は None として扱う
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


def _build_aggregate_sql(bango_col: str, bamei_col: str, role: str, now: str) -> str:
    """集計クエリを構築する（role ごとに固定カラムを使用）"""
    # tansho_odds は '0042' = 4.2倍 の形式。int(odds)/10 が実オッズ。
    # 単勝回収率 = Σ(1着時のオッズ×100) / (出走回数×100) = Σ(1着時のオッズ) / 出走回数
    # kakutei_chakujun が '00' や空 は除外、ijo_kubun_code が '0'（正常）のみ
    return f"""
        INSERT OR REPLACE INTO sire_stats
            (hanshoku_bango, role, bamei, runners, starts, wins, second, third,
             win_rate, rentai_rate, fukusho_rate, tansho_roi, updated_at)
        SELECT
            m.{bango_col},
            '{role}',
            m.{bamei_col},
            COUNT(DISTINCT ru.ketto_toroku_bango) AS runners,
            COUNT(*) AS starts,
            SUM(CASE WHEN ru.kakutei_chakujun = '01' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN ru.kakutei_chakujun = '02' THEN 1 ELSE 0 END) AS second,
            SUM(CASE WHEN ru.kakutei_chakujun = '03' THEN 1 ELSE 0 END) AS third,
            CAST(SUM(CASE WHEN ru.kakutei_chakujun = '01' THEN 1 ELSE 0 END) AS REAL)
                / COUNT(*),
            CAST(SUM(CASE WHEN ru.kakutei_chakujun IN ('01','02') THEN 1 ELSE 0 END) AS REAL)
                / COUNT(*),
            CAST(SUM(CASE WHEN ru.kakutei_chakujun IN ('01','02','03') THEN 1 ELSE 0 END) AS REAL)
                / COUNT(*),
            CAST(
                SUM(CASE
                    WHEN ru.kakutei_chakujun = '01' AND ru.tansho_odds IS NOT NULL
                        AND ru.tansho_odds != '' AND CAST(ru.tansho_odds AS INTEGER) > 0
                    THEN CAST(ru.tansho_odds AS INTEGER) * 10.0
                    ELSE 0
                END)
                / (COUNT(*) * 100.0)
            AS REAL),
            '{now}'
        FROM _tmp_uma_sire m
        JOIN jvd_race_uma ru ON m.ketto_toroku_bango = ru.ketto_toroku_bango
        WHERE m.{bango_col} IS NOT NULL
          AND m.{bango_col} != ''
          AND ru.kakutei_chakujun IS NOT NULL
          AND ru.kakutei_chakujun != ''
          AND ru.kakutei_chakujun != '00'
          AND (ru.ijo_kubun_code IS NULL OR ru.ijo_kubun_code = '0')
        GROUP BY m.{bango_col}
    """


# role ごとに固定SQLを事前定義（S608 対策: 外部入力がSQL構築に混入しない）
_AGGREGATE_SQLS = {
    "sire": lambda now: _build_aggregate_sql("sire_bango", "sire_bamei", "sire", now),
    "bms": lambda now: _build_aggregate_sql("bms_bango", "bms_bamei", "bms", now),
}


def aggregate_stats(conn: sqlite3.Connection, role: str) -> int:
    """種牡馬 or BMS の成績を集計して sire_stats に INSERT"""
    if role not in _AGGREGATE_SQLS:
        raise ValueError(f"不正な role: {role}")

    logger.info(f"集計中: role={role}")
    now = datetime.now(timezone.utc).isoformat()
    sql = _AGGREGATE_SQLS[role](now)

    cursor = conn.execute(sql)
    conn.commit()
    count = cursor.rowcount
    logger.info(f"集計完了: role={role}, {count:,} 種牡馬/BMS")
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

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    start = time.time()

    try:
        create_tables(conn)
        build_sire_mapping(conn)

        # インデックスを作成（集計クエリ高速化）
        logger.info("一時テーブルにインデックス作成中...")
        conn.execute("CREATE INDEX IF NOT EXISTS _idx_tmp_sire ON _tmp_uma_sire (sire_bango)")
        conn.execute("CREATE INDEX IF NOT EXISTS _idx_tmp_bms ON _tmp_uma_sire (bms_bango)")
        conn.commit()

        aggregate_stats(conn, "sire")
        aggregate_stats(conn, "bms")

        # 結果サマリー
        row = conn.execute("SELECT role, COUNT(*), SUM(starts), SUM(wins) FROM sire_stats GROUP BY role").fetchall()
        logger.info("=== 集計結果サマリー ===")
        for r in row:
            logger.info(f"  {r[0]}: {r[1]:,} 頭, 延べ {r[2]:,} 走, {r[3]:,} 勝")

    finally:
        cleanup(conn)
        conn.close()

    elapsed = time.time() - start
    logger.info(f"完了 ({elapsed:.1f}秒)")


if __name__ == "__main__":
    main()

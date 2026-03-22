"""
人的要素（騎手・調教師・馬主・生産者）成績集計バッチ

jvd_race_uma から騎手・調教師・馬主の成績を直接集計し、
生産者は jvd_uma と JOIN して成績を集計する。
結果は human_factor_stats テーブルに格納。

ワークテーブル (human_factor_stats_new) に全件書き込み後、
アトミックにRENAMEすることで集計中の不完全データ公開を防ぐ。

使い方:
    python backend/batch/calc_human_factor_stats.py
"""

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

WORK_TABLE = "human_factor_stats_new"

# 出走回数の最低閾値（これ未満はノイズとして除外）
_MIN_STARTS_FILTER = 0  # 集計時は全件INSERT、スコア計算時にフィルタする


def create_work_table(conn: sqlite3.Connection) -> None:
    """ワークテーブルを作成（既存があれば再作成）"""
    conn.execute(f"DROP TABLE IF EXISTS {WORK_TABLE}")
    conn.execute(f"""
        CREATE TABLE {WORK_TABLE} (
            person_code     TEXT NOT NULL,
            role            TEXT NOT NULL,
            person_name     TEXT,
            starts          INTEGER DEFAULT 0,
            wins            INTEGER DEFAULT 0,
            second          INTEGER DEFAULT 0,
            third           INTEGER DEFAULT 0,
            win_rate        REAL DEFAULT 0,
            rentai_rate     REAL DEFAULT 0,
            fukusho_rate    REAL DEFAULT 0,
            tansho_roi      REAL DEFAULT 0,
            updated_at      TEXT,
            PRIMARY KEY (person_code, role)
        )
    """)
    conn.commit()


def _base_where_clause() -> str:
    """共通のWHERE条件（正常完走のみ対象）"""
    return (
        "ru.kakutei_chakujun IS NOT NULL "
        "AND ru.kakutei_chakujun != '' "
        "AND ru.kakutei_chakujun != '00' "
        "AND (ru.ijo_kubun_code IS NULL OR ru.ijo_kubun_code = '0')"
    )


def _tansho_roi_expr() -> str:
    """単勝回収率の計算式（共通）"""
    return (
        "CAST("
        "  SUM(CASE"
        "    WHEN ru.kakutei_chakujun = '01' AND ru.tansho_odds IS NOT NULL"
        "      AND ru.tansho_odds != '' AND CAST(ru.tansho_odds AS INTEGER) > 0"
        "    THEN CAST(ru.tansho_odds AS INTEGER) * 10.0"
        "    ELSE 0"
        "  END)"
        "  / (COUNT(*) * 100.0)"
        " AS REAL)"
    )


def aggregate_jockey(conn: sqlite3.Connection, now: str) -> int:
    """騎手（jockey）の成績を集計"""
    logger.info("集計中: role=jockey（騎手）")
    sql = f"""
        INSERT INTO {WORK_TABLE}
            (person_code, role, person_name, starts, wins, second, third,
             win_rate, rentai_rate, fukusho_rate, tansho_roi, updated_at)
        SELECT
            ru.kishu_code,
            'jockey',
            ru.kishu_mei_ryakusho,
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
            {_tansho_roi_expr()},
            '{now}'
        FROM jvd_race_uma ru
        WHERE ru.kishu_code IS NOT NULL AND ru.kishu_code != ''
          AND {_base_where_clause()}
        GROUP BY ru.kishu_code
    """
    cursor = conn.execute(sql)
    count = cursor.rowcount
    logger.info(f"集計完了: role=jockey, {count:,} 名")
    return count


def aggregate_trainer(conn: sqlite3.Connection, now: str) -> int:
    """調教師（trainer）の成績を集計"""
    logger.info("集計中: role=trainer（調教師）")
    sql = f"""
        INSERT INTO {WORK_TABLE}
            (person_code, role, person_name, starts, wins, second, third,
             win_rate, rentai_rate, fukusho_rate, tansho_roi, updated_at)
        SELECT
            ru.chokyoshi_code,
            'trainer',
            ru.chokyoshi_mei_ryakusho,
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
            {_tansho_roi_expr()},
            '{now}'
        FROM jvd_race_uma ru
        WHERE ru.chokyoshi_code IS NOT NULL AND ru.chokyoshi_code != ''
          AND {_base_where_clause()}
        GROUP BY ru.chokyoshi_code
    """
    cursor = conn.execute(sql)
    count = cursor.rowcount
    logger.info(f"集計完了: role=trainer, {count:,} 名")
    return count


def aggregate_owner(conn: sqlite3.Connection, now: str) -> int:
    """馬主（owner）の成績を集計"""
    logger.info("集計中: role=owner（馬主）")
    sql = f"""
        INSERT INTO {WORK_TABLE}
            (person_code, role, person_name, starts, wins, second, third,
             win_rate, rentai_rate, fukusho_rate, tansho_roi, updated_at)
        SELECT
            ru.banushi_code,
            'owner',
            ru.banushi_mei,
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
            {_tansho_roi_expr()},
            '{now}'
        FROM jvd_race_uma ru
        WHERE ru.banushi_code IS NOT NULL AND ru.banushi_code != ''
          AND {_base_where_clause()}
        GROUP BY ru.banushi_code
    """
    cursor = conn.execute(sql)
    count = cursor.rowcount
    logger.info(f"集計完了: role=owner, {count:,} 名/社")
    return count


def aggregate_breeder(conn: sqlite3.Connection, now: str) -> int:
    """生産者（breeder）の成績を集計（jvd_uma JOIN）"""
    logger.info("集計中: role=breeder（生産者）")
    sql = f"""
        INSERT INTO {WORK_TABLE}
            (person_code, role, person_name, starts, wins, second, third,
             win_rate, rentai_rate, fukusho_rate, tansho_roi, updated_at)
        SELECT
            u.seisansha_code,
            'breeder',
            u.seisansha_mei,
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
            {_tansho_roi_expr()},
            '{now}'
        FROM jvd_race_uma ru
        JOIN jvd_uma u ON ru.ketto_toroku_bango = u.ketto_toroku_bango
        WHERE u.seisansha_code IS NOT NULL AND u.seisansha_code != ''
          AND {_base_where_clause()}
        GROUP BY u.seisansha_code
    """
    cursor = conn.execute(sql)
    count = cursor.rowcount
    logger.info(f"集計完了: role=breeder, {count:,} 社")
    return count


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
        create_work_table(conn)
        now = datetime.now(timezone.utc).isoformat()

        aggregate_jockey(conn, now)
        aggregate_trainer(conn, now)
        aggregate_owner(conn, now)
        aggregate_breeder(conn, now)

        # ワークテーブル → 本テーブルにアトミックswap
        conn.execute("DROP TABLE IF EXISTS human_factor_stats")
        conn.execute(f"ALTER TABLE {WORK_TABLE} RENAME TO human_factor_stats")
        conn.commit()
        logger.info("テーブル swap 完了: human_factor_stats_new → human_factor_stats")

        # 結果サマリー
        rows = conn.execute(
            "SELECT role, COUNT(*), SUM(starts), SUM(wins) "
            "FROM human_factor_stats GROUP BY role"
        ).fetchall()
        logger.info("=== 集計結果サマリー ===")
        for r in rows:
            logger.info(f"  {r[0]}: {r[1]:,} 名/社, 延べ {r[2]:,} 走, {r[3]:,} 勝")

    except Exception:
        # 失敗時はワークテーブルを掃除
        conn.execute(f"DROP TABLE IF EXISTS {WORK_TABLE}")
        conn.commit()
        raise
    finally:
        conn.close()

    elapsed = time.time() - start
    logger.info(f"完了 ({elapsed:.1f}秒)")


if __name__ == "__main__":
    main()

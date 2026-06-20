"""
カテゴリE（コンディション）E1-枠順・E2-斤量 成績集計バッチ

新馬戦（競走条件コード 701）のみを対象に、枠順・斤量別の成績を集計する。
- draw_stats:   競馬場 × 距離帯 × 枠番 ごとの勝率/単勝回収率（E1-枠順）
- weight_stats: 斤量(負担重量)ごとの勝率/単勝回収率（E2-斤量）

新馬戦は馬自身の過去走が無いため、馬の属性ではなく「枠・斤量という条件側の
ベース成績」をスコア化の土台に使う。

ワークテーブルに全件書き込み後アトミックにRENAMEし、集計中の不完全データ公開を防ぐ。

使い方:
    python backend/batch/calc_condition_stats.py
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

WORK_DRAW = "draw_stats_new"
WORK_WEIGHT = "weight_stats_new"

# 新馬戦判定: 2歳/3歳いずれかの競走条件コードが '701'
# （新馬戦は2歳枠だけでなく3歳枠にも存在するため両方を見る）
MAIDEN_FILTER = "'701' IN (r.kyoso_joken_code_2sai, r.kyoso_joken_code_3sai)"

# 距離帯 — カテゴリBと同一区分
DISTANCE_CASE = """
    CASE WHEN CAST(r.kyori AS INTEGER) <= 1400 THEN 'sprint'
         WHEN CAST(r.kyori AS INTEGER) <= 1800 THEN 'mile'
         WHEN CAST(r.kyori AS INTEGER) <= 2200 THEN 'middle'
         ELSE 'long'
    END
"""

# jvd_race_uma × jvd_race のJOIN（6カラム複合PK）
RACE_JOIN = """
    FROM jvd_race_uma ru
    JOIN jvd_race r ON ru.kaisai_nen = r.kaisai_nen
        AND ru.kaisai_tsukihi = r.kaisai_tsukihi
        AND ru.keibajo_code = r.keibajo_code
        AND ru.kaisai_kai = r.kaisai_kai
        AND ru.kaisai_nichime = r.kaisai_nichime
        AND ru.race_bango = r.race_bango
"""


def _base_where_clause() -> str:
    """正常完走のみ対象（人的要素バッチと同条件）"""
    return (
        "ru.kakutei_chakujun IS NOT NULL "
        "AND ru.kakutei_chakujun != '' "
        "AND ru.kakutei_chakujun != '00' "
        "AND (ru.ijo_kubun_code IS NULL OR ru.ijo_kubun_code = '0')"
    )


def _tansho_roi_expr() -> str:
    """単勝回収率の計算式（人的要素バッチと同式）。tansho_odds は実オッズ×10 で格納。"""
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


def create_work_tables(conn: sqlite3.Connection) -> None:
    """ワークテーブルを作成（既存があれば再作成）"""
    conn.execute(f"DROP TABLE IF EXISTS {WORK_DRAW}")
    conn.execute(f"""
        CREATE TABLE {WORK_DRAW} (
            keibajo_code    TEXT NOT NULL,
            distance_band   TEXT NOT NULL,
            wakuban         TEXT NOT NULL,
            starts          INTEGER DEFAULT 0,
            wins            INTEGER DEFAULT 0,
            win_rate        REAL DEFAULT 0,
            tansho_roi      REAL DEFAULT 0,
            updated_at      TEXT,
            PRIMARY KEY (keibajo_code, distance_band, wakuban)
        )
    """)
    conn.execute(f"DROP TABLE IF EXISTS {WORK_WEIGHT}")
    conn.execute(f"""
        CREATE TABLE {WORK_WEIGHT} (
            futan_juryo     TEXT NOT NULL,
            starts          INTEGER DEFAULT 0,
            wins            INTEGER DEFAULT 0,
            win_rate        REAL DEFAULT 0,
            tansho_roi      REAL DEFAULT 0,
            updated_at      TEXT,
            PRIMARY KEY (futan_juryo)
        )
    """)
    conn.commit()


def aggregate_draw(conn: sqlite3.Connection, now: str) -> int:
    """E1: 競馬場 × 距離帯 × 枠番 別の成績（新馬戦のみ・芝/ダートのみ）"""
    logger.info("集計中: draw_stats（E1-枠順）")
    sql = f"""
        INSERT INTO {WORK_DRAW}
            (keibajo_code, distance_band, wakuban, starts, wins, win_rate, tansho_roi, updated_at)
        SELECT
            r.keibajo_code,
            {DISTANCE_CASE} AS distance_band,
            ru.wakuban,
            COUNT(*) AS starts,
            SUM(CASE WHEN ru.kakutei_chakujun = '01' THEN 1 ELSE 0 END) AS wins,
            CAST(SUM(CASE WHEN ru.kakutei_chakujun = '01' THEN 1 ELSE 0 END) AS REAL)
                / COUNT(*),
            {_tansho_roi_expr()},
            '{now}'
        {RACE_JOIN}
        WHERE {MAIDEN_FILTER}
          AND ru.wakuban IS NOT NULL AND ru.wakuban != ''
          AND SUBSTR(r.track_code, 1, 1) IN ('1', '2')
          AND {_base_where_clause()}
        GROUP BY r.keibajo_code, distance_band, ru.wakuban
    """
    count = conn.execute(sql).rowcount
    logger.info(f"集計完了: draw_stats, {count:,} セル")
    return count


def aggregate_weight(conn: sqlite3.Connection, now: str) -> int:
    """E2: 斤量(負担重量)別の成績（新馬戦のみ）"""
    logger.info("集計中: weight_stats（E2-斤量）")
    sql = f"""
        INSERT INTO {WORK_WEIGHT}
            (futan_juryo, starts, wins, win_rate, tansho_roi, updated_at)
        SELECT
            ru.futan_juryo,
            COUNT(*) AS starts,
            SUM(CASE WHEN ru.kakutei_chakujun = '01' THEN 1 ELSE 0 END) AS wins,
            CAST(SUM(CASE WHEN ru.kakutei_chakujun = '01' THEN 1 ELSE 0 END) AS REAL)
                / COUNT(*),
            {_tansho_roi_expr()},
            '{now}'
        {RACE_JOIN}
        WHERE {MAIDEN_FILTER}
          AND ru.futan_juryo IS NOT NULL AND ru.futan_juryo != ''
          AND {_base_where_clause()}
        GROUP BY ru.futan_juryo
    """
    count = conn.execute(sql).rowcount
    logger.info(f"集計完了: weight_stats, {count:,} 種別")
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
        create_work_tables(conn)
        now = datetime.now(timezone.utc).isoformat()

        aggregate_draw(conn, now)
        aggregate_weight(conn, now)

        # ワークテーブル → 本テーブルにアトミックswap
        conn.execute("DROP TABLE IF EXISTS draw_stats")
        conn.execute(f"ALTER TABLE {WORK_DRAW} RENAME TO draw_stats")
        conn.execute("DROP TABLE IF EXISTS weight_stats")
        conn.execute(f"ALTER TABLE {WORK_WEIGHT} RENAME TO weight_stats")
        conn.commit()
        logger.info("テーブル swap 完了: draw_stats / weight_stats")

        # 結果サマリー
        d = conn.execute("SELECT COUNT(*), SUM(starts), SUM(wins) FROM draw_stats").fetchone()
        w = conn.execute("SELECT COUNT(*), SUM(starts), SUM(wins) FROM weight_stats").fetchone()
        logger.info("=== 集計結果サマリー ===")
        logger.info(f"  draw_stats:   {d[0]:,} セル, 延べ {d[1]:,} 走, {d[2]:,} 勝")
        logger.info(f"  weight_stats: {w[0]:,} 種別, 延べ {w[1]:,} 走, {w[2]:,} 勝")

    except Exception:
        # 失敗時はワークテーブルを掃除
        conn.execute(f"DROP TABLE IF EXISTS {WORK_DRAW}")
        conn.execute(f"DROP TABLE IF EXISTS {WORK_WEIGHT}")
        conn.commit()
        raise
    finally:
        conn.close()

    elapsed = time.time() - start
    logger.info(f"完了 ({elapsed:.1f}秒)")


if __name__ == "__main__":
    main()

"""
JV-Data SQLite インポーター

jvdata_parser でパースした辞書データを backend/jvdata_schema.sql の
テーブルに INSERT/UPSERT する。

使い方:
    python scraper/jvdata_importer.py [--year YEAR]
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

# 同一パッケージのパーサーをインポート
sys.path.insert(0, os.path.dirname(__file__))
from jvdata_parser import parse_directory, parse_file


# デフォルトパス
TFJV_DIR = "D:/TFJV"
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "backend", "bloodline.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "backend", "jvdata_schema.sql")


class JVDataImporter:
    """JV-Data → SQLite インポーター"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = os.path.abspath(db_path)
        self.conn = None

    def connect(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def init_schema(self):
        """jvdata_schema.sql を実行してテーブルを作成"""
        schema_path = os.path.abspath(SCHEMA_PATH)
        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()
        self.conn.executescript(schema_sql)
        print(f"スキーマ初期化完了: {schema_path}")

    # ================================================================
    # UPSERT ヘルパー
    # ================================================================

    def _upsert(self, table: str, data: dict, key_columns: list[str]):
        """INSERT OR REPLACE"""
        columns = list(data.keys())
        placeholders = ", ".join(["?"] * len(columns))
        col_names = ", ".join(columns)
        sql = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"
        values = [data[c] for c in columns]
        self.conn.execute(sql, values)

    def _filter_columns(self, data: dict, table_columns: list[str]) -> dict:
        """テーブルに存在するカラムのみ抽出"""
        return {k: v for k, v in data.items() if k in table_columns}

    def _get_table_columns(self, table: str) -> list[str]:
        """テーブルのカラム一覧を取得"""
        cursor = self.conn.execute(f"PRAGMA table_info({table})")
        return [row[1] for row in cursor.fetchall()]

    # ================================================================
    # レコード種別ごとのインポート
    # ================================================================

    def import_ra(self, records: list[dict]):
        """RA（レース詳細）をインポート"""
        columns = self._get_table_columns("jvd_race")
        count = 0
        for rec in records:
            if rec.get("_record_type") != "RA":
                continue
            data = self._filter_columns(rec, columns)
            self._upsert("jvd_race", data, [
                "kaisai_nen", "kaisai_tsukihi", "keibajo_code",
                "kaisai_kai", "kaisai_nichime", "race_bango"
            ])
            count += 1
        self.conn.commit()
        return count

    def import_se(self, records: list[dict]):
        """SE（馬毎レース情報）をインポート"""
        columns = self._get_table_columns("jvd_race_uma")
        count = 0
        for rec in records:
            if rec.get("_record_type") != "SE":
                continue
            data = self._filter_columns(rec, columns)
            self._upsert("jvd_race_uma", data, [
                "kaisai_nen", "kaisai_tsukihi", "keibajo_code",
                "kaisai_kai", "kaisai_nichime", "race_bango",
                "umaban", "ketto_toroku_bango"
            ])
            count += 1
        self.conn.commit()
        return count

    def import_um(self, records: list[dict]):
        """UM（競走馬マスタ）をインポート"""
        columns = self._get_table_columns("jvd_uma")
        count = 0
        for rec in records:
            if rec.get("_record_type") != "UM":
                continue
            # 3代血統情報をJSON化
            sandai = []
            for i in range(1, 15):
                bango = rec.get(f"ketto_hanshoku_bango_{i:02d}", "")
                bamei = rec.get(f"ketto_bamei_{i:02d}", "")
                sandai.append({"hanshoku_toroku_bango": bango, "bamei": bamei})
            data = self._filter_columns(rec, columns)
            data["sandai_ketto"] = str(sandai)  # JSON文字列として格納

            # 着回数をJSON化
            for prefix_group, json_col in [
                (["sogo_chakukaisu"], "sogo_chakukaisu"),
                (["chuo_chakukaisu"], "chuo_gokei_chakukaisu"),
            ]:
                vals = []
                for prefix in prefix_group:
                    for suffix in ["_1chaku", "_2chaku", "_3chaku", "_4chaku", "_5chaku", "_chakugai"]:
                        key = f"{prefix}{suffix}"
                        vals.append(rec.get(key, "0"))
                data[json_col] = str(vals)

            self._upsert("jvd_uma", data, ["ketto_toroku_bango"])
            count += 1
        self.conn.commit()
        return count

    def import_hn(self, records: list[dict]):
        """HN（繁殖馬マスタ）をインポート"""
        columns = self._get_table_columns("jvd_hanshoku")
        count = 0
        for rec in records:
            if rec.get("_record_type") != "HN":
                continue
            data = self._filter_columns(rec, columns)
            self._upsert("jvd_hanshoku", data, ["hanshoku_toroku_bango"])
            count += 1
        self.conn.commit()
        return count

    def import_sk(self, records: list[dict]):
        """SK（産駒マスタ）をインポート"""
        columns = self._get_table_columns("jvd_sanku")
        count = 0
        for rec in records:
            if rec.get("_record_type") != "SK":
                continue
            # 3代血統番号をJSON化
            sandai = []
            for i in range(1, 15):
                bango = rec.get(f"sandai_hanshoku_{i:02d}", "")
                sandai.append(bango)
            data = self._filter_columns(rec, columns)
            data["sandai_ketto_hanshoku"] = str(sandai)
            self._upsert("jvd_sanku", data, ["ketto_toroku_bango"])
            count += 1
        self.conn.commit()
        return count

    # ================================================================
    # 一括インポート
    # ================================================================

    def import_year(self, year: int = 2024):
        """指定年のデータを一括インポート"""
        t0 = time.time()
        print(f"\n{'='*60}")
        print(f"JV-Data インポート開始: {year}年")
        print(f"DB: {self.db_path}")
        print(f"{'='*60}\n")

        # RA（レース詳細）
        print("RA（レース詳細）をパース中...")
        ra_recs = parse_directory(f"{TFJV_DIR}/SE_DATA", "SR*.DAT", {"RA"}, year=year)
        n = self.import_ra(ra_recs)
        print(f"  → {n}件 インポート完了")

        # SE（馬毎レース情報）
        print("SE（馬毎レース情報）をパース中...")
        se_recs = parse_directory(f"{TFJV_DIR}/SE_DATA", "SU*.DAT", {"SE"}, year=year)
        n = self.import_se(se_recs)
        print(f"  → {n}件 インポート完了")

        # UM（競走馬マスタ）
        print("UM（競走馬マスタ）をパース中...")
        um_recs = parse_directory(f"{TFJV_DIR}/UM_DATA", "UM*.DAT", {"UM"}, year=year)
        n = self.import_um(um_recs)
        print(f"  → {n}件 インポート完了")

        # SK（産駒マスタ）
        print("SK（産駒マスタ）をパース中...")
        sk_recs = parse_directory(f"{TFJV_DIR}/UM_DATA", "SK*.DAT", {"SK"}, year=year)
        n = self.import_sk(sk_recs)
        print(f"  → {n}件 インポート完了")

        # HN（繁殖馬マスタ）— 年フォルダなし、全件
        print("HN（繁殖馬マスタ）をパース中...")
        hn_recs = []
        kt_dir = Path(f"{TFJV_DIR}/KT_DATA")
        for fpath in sorted(kt_dir.glob("KT2*.DAT")):
            hn_recs.extend(parse_file(str(fpath), {"HN"}))
        n = self.import_hn(hn_recs)
        print(f"  → {n}件 インポート完了")

        elapsed = time.time() - t0
        print(f"\n全インポート完了（{elapsed:.1f}秒）")
        self._print_summary()

    def _print_summary(self):
        """各テーブルのレコード数を表示"""
        tables = ["jvd_race", "jvd_race_uma", "jvd_uma", "jvd_sanku", "jvd_hanshoku"]
        print(f"\n{'テーブル':<20} {'件数':>10}")
        print("-" * 32)
        for table in tables:
            try:
                cur = self.conn.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                print(f"{table:<20} {count:>10}")
            except sqlite3.OperationalError:
                print(f"{table:<20} {'(未作成)':>10}")


def main():
    parser = argparse.ArgumentParser(description="JV-Data → SQLite インポーター")
    parser.add_argument("--year", type=int, default=2024, help="インポート対象年 (default: 2024)")
    parser.add_argument("--db", type=str, default=DB_PATH, help="SQLiteデータベースパス")
    args = parser.parse_args()

    importer = JVDataImporter(db_path=args.db)
    importer.connect()
    importer.init_schema()
    importer.import_year(year=args.year)
    importer.close()


if __name__ == "__main__":
    main()

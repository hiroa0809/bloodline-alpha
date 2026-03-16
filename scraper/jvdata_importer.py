"""
JV-Data SQLite インポーター

jvdata_parser でパースした辞書データを backend/jvdata_schema.sql の
テーブルに INSERT/UPSERT する。

使い方:
    # 単年インポート（従来互換）
    python scraper/jvdata_importer.py --year 2024

    # 全年一括インポート（SE_DATA内の全年フォルダを自動検出）
    python scraper/jvdata_importer.py --all-years --resume --log-file backend/logs/jvdata_import.log

    # 年範囲指定
    python scraper/jvdata_importer.py --start-year 1990 --end-year 2000
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# 同一パッケージのパーサーをインポート
sys.path.insert(0, os.path.dirname(__file__))
from jvdata_parser import parse_directory, parse_file


# デフォルトパス
TFJV_DIR = "D:/TFJV"
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "backend", "bloodline.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "backend", "jvdata_schema.sql")


def setup_logging(log_file: str) -> logging.Logger:
    """stdout とファイルの両方に出力するロガーを初期化する"""
    os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
    logger = logging.getLogger("jvdata_importer")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


class JVDataImporter:
    """JV-Data → SQLite インポーター"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = os.path.abspath(db_path)
        self.conn = None
        self.logger = logging.getLogger("jvdata_importer")

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
        self.logger.info(f"スキーマ初期化完了: {schema_path}")

    # ================================================================
    # UPSERT ヘルパー
    # ================================================================

    def _upsert(self, table: str, data: dict):
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
            self._upsert("jvd_race", data)
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
            self._upsert("jvd_race_uma", data)
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

            self._upsert("jvd_uma", data)
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
            self._upsert("jvd_hanshoku", data)
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
            self._upsert("jvd_sanku", data)
            count += 1
        self.conn.commit()
        return count

    # ================================================================
    # 全年インポート用メソッド
    # ================================================================

    @staticmethod
    def detect_available_years(data_dir: str) -> list[int]:
        """指定フォルダ内の4桁数字サブディレクトリを昇順で列挙する"""
        base = Path(data_dir)
        years = []
        for entry in sorted(base.iterdir()):
            if entry.is_dir() and entry.name.isdigit() and len(entry.name) == 4:
                years.append(int(entry.name))
        return years

    def is_year_imported(self, year: int) -> bool:
        """jvd_race テーブルに該当年のレコードが1件以上存在するか確認"""
        try:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM jvd_race WHERE kaisai_nen = ?",
                (str(year),)
            )
            return cur.fetchone()[0] > 0
        except sqlite3.OperationalError:
            return False

    def import_hn_all(self) -> int:
        """HN（繁殖馬マスタ）を KT_DATA から全件インポート（1回のみ実行）"""
        self.logger.info("HN（繁殖馬マスタ）インポート開始...")
        hn_recs = []
        kt_dir = Path(f"{TFJV_DIR}/KT_DATA")
        for fpath in sorted(kt_dir.glob("KT2*.DAT")):
            hn_recs.extend(parse_file(str(fpath), {"HN"}))
        n = self.import_hn(hn_recs)
        self.logger.info(f"HN（繁殖馬マスタ）: {n:,}件 インポート完了")
        return n

    def import_all_years(self, years: list[int], resume: bool = False) -> dict:
        """年リストを順にインポートする。エラーは年単位でスキップして継続する"""
        total_start = time.time()
        success_years = []
        failed_years = []
        skipped_years = []
        grand_total = {"RA": 0, "SE": 0, "UM": 0, "SK": 0}

        if not years:
            self.logger.warning("対象年が空です")
            return {"success": [], "failed": [], "skipped": [], "totals": grand_total}

        self.logger.info(f"全年インポート開始: {len(years)}年分 ({years[0]}〜{years[-1]})")
        if resume:
            self.logger.info("--resume モード有効: インポート済み年はスキップします")

        for i, year in enumerate(years, 1):
            self.logger.info(f"[{i}/{len(years)}] {year}年 処理開始")

            if resume and self.is_year_imported(year):
                self.logger.info(f"  → スキップ（インポート済み）")
                skipped_years.append(year)
                continue

            result = self.import_year(year)

            if result is None:
                failed_years.append(year)
                self.logger.error(f"  → {year}年 失敗（次の年へ継続）")
            else:
                success_years.append(year)
                for k in grand_total:
                    grand_total[k] += result.get(k, 0)

        # 最終サマリー
        elapsed = time.time() - total_start
        self.logger.info("=" * 60)
        self.logger.info("全年インポート完了サマリー")
        self.logger.info(f"  処理時間    : {elapsed:.1f}秒 ({elapsed / 60:.1f}分)")
        self.logger.info(f"  成功年数    : {len(success_years)}年")
        self.logger.info(f"  スキップ年数: {len(skipped_years)}年（--resume）")
        self.logger.info(f"  失敗年数    : {len(failed_years)}年")
        if failed_years:
            self.logger.error(f"  失敗年一覧  : {failed_years}")
        self.logger.info("  総インポート件数:")
        for k, v in grand_total.items():
            self.logger.info(f"    {k}: {v:,}件")
        self.logger.info("=" * 60)
        self._log_summary()

        return {
            "success": success_years,
            "failed": failed_years,
            "skipped": skipped_years,
            "totals": grand_total,
        }

    # ================================================================
    # 単年インポート
    # ================================================================

    def import_year(self, year: int) -> dict | None:
        """指定年の RA/SE/UM/SK データをインポートする。失敗時は None を返す"""
        t0 = time.time()
        self.logger.info(f"{'=' * 50}")
        self.logger.info(f"{year}年 インポート開始")
        counts = {}

        try:
            # RA（レース詳細）
            self.logger.info(f"RA（レース詳細）パース中... [{year}]")
            try:
                ra_recs = parse_directory(f"{TFJV_DIR}/SE_DATA", "SR*.DAT", {"RA"}, year=year)
                counts["RA"] = self.import_ra(ra_recs)
                self.logger.info(f"  RA: {counts['RA']:,}件")
            except FileNotFoundError:
                self.logger.warning(f"  RA: SE_DATA/{year} フォルダなし — スキップ")
                counts["RA"] = 0

            # SE（馬毎レース情報）
            self.logger.info(f"SE（馬毎レース情報）パース中... [{year}]")
            try:
                se_recs = parse_directory(f"{TFJV_DIR}/SE_DATA", "SU*.DAT", {"SE"}, year=year)
                counts["SE"] = self.import_se(se_recs)
                self.logger.info(f"  SE: {counts['SE']:,}件")
            except FileNotFoundError:
                self.logger.warning(f"  SE: SE_DATA/{year} フォルダなし — スキップ")
                counts["SE"] = 0

            # UM（競走馬マスタ）
            self.logger.info(f"UM（競走馬マスタ）パース中... [{year}]")
            try:
                um_recs = parse_directory(f"{TFJV_DIR}/UM_DATA", "UM*.DAT", {"UM"}, year=year)
                counts["UM"] = self.import_um(um_recs)
                self.logger.info(f"  UM: {counts['UM']:,}件")
            except FileNotFoundError:
                self.logger.warning(f"  UM: UM_DATA/{year} フォルダなし — スキップ")
                counts["UM"] = 0

            # SK（産駒マスタ）
            self.logger.info(f"SK（産駒マスタ）パース中... [{year}]")
            try:
                sk_recs = parse_directory(f"{TFJV_DIR}/UM_DATA", "SK*.DAT", {"SK"}, year=year)
                counts["SK"] = self.import_sk(sk_recs)
                self.logger.info(f"  SK: {counts['SK']:,}件")
            except FileNotFoundError:
                self.logger.warning(f"  SK: UM_DATA/{year} フォルダなし — スキップ")
                counts["SK"] = 0

            elapsed = time.time() - t0
            self.logger.info(
                f"{year}年 完了（{elapsed:.1f}秒）"
                f" RA={counts.get('RA', 0):,} SE={counts.get('SE', 0):,}"
                f" UM={counts.get('UM', 0):,} SK={counts.get('SK', 0):,}"
            )
            return counts

        except Exception as e:
            elapsed = time.time() - t0
            self.logger.error(f"{year}年 インポート失敗（{elapsed:.1f}秒）: {e}", exc_info=True)
            return None

    def _log_summary(self):
        """各テーブルのレコード数をログ出力"""
        tables = ["jvd_race", "jvd_race_uma", "jvd_uma", "jvd_sanku", "jvd_hanshoku"]
        self.logger.info(f"{'テーブル':<20} {'件数':>12}")
        self.logger.info("-" * 34)
        for table in tables:
            try:
                cur = self.conn.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                self.logger.info(f"  {table:<20} {count:>12,}")
            except sqlite3.OperationalError:
                self.logger.warning(f"  {table:<20} {'(未作成)':>12}")


def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_log = os.path.join(
        os.path.dirname(__file__), "..", "backend", "logs",
        f"jvdata_import_{ts}.log"
    )

    parser = argparse.ArgumentParser(description="JV-Data → SQLite インポーター")

    # モード（--year と --all-years は排他）
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--year", type=int, help="単年インポート (例: 2024)")
    mode_group.add_argument("--all-years", action="store_true",
                            help="SE_DATA 内の全年フォルダを自動検出してインポート")

    # 年範囲指定（--all-years とは別途使用可）
    parser.add_argument("--start-year", type=int, help="インポート開始年")
    parser.add_argument("--end-year", type=int, help="インポート終了年")

    # オプション
    parser.add_argument("--resume", action="store_true",
                        help="インポート済み年をスキップ（jvd_race のレコード有無で判定）")
    parser.add_argument("--skip-hn", action="store_true",
                        help="HN（繁殖馬マスタ）インポートをスキップ（再実行時用）")
    parser.add_argument("--db", type=str, default=DB_PATH, help="SQLiteデータベースパス")
    parser.add_argument("--log-file", type=str, default=default_log, help="ログファイルパス")

    args = parser.parse_args()

    # バリデーション
    if args.all_years and (args.start_year or args.end_year):
        parser.error("--all-years と --start-year/--end-year は同時指定できません")

    # ロギング初期化
    logger = setup_logging(args.log_file)
    logger.info(f"ログファイル: {os.path.abspath(args.log_file)}")
    logger.info(f"DB: {os.path.abspath(args.db)}")

    # インポーター起動
    importer = JVDataImporter(db_path=args.db)
    importer.connect()
    importer.init_schema()

    if args.all_years or args.start_year or args.end_year:
        # 全年または年範囲モード
        all_years = JVDataImporter.detect_available_years(f"{TFJV_DIR}/SE_DATA")
        years = [
            y for y in all_years
            if (args.start_year is None or y >= args.start_year)
            and (args.end_year is None or y <= args.end_year)
        ]
        if not years:
            logger.error("対象年が見つかりませんでした。--start-year / --end-year の値を確認してください")
            importer.close()
            sys.exit(1)

        logger.info(f"対象年: {years[0]}〜{years[-1]} ({len(years)}年分)")

        if not args.skip_hn:
            importer.import_hn_all()
        else:
            logger.info("--skip-hn: HN インポートをスキップ")

        importer.import_all_years(years, resume=args.resume)

    else:
        # 単年モード（従来互換）
        year = args.year or 2024
        if not args.skip_hn:
            importer.import_hn_all()
        importer.import_year(year=year)
        importer._log_summary()

    importer.close()
    logger.info("処理終了")


if __name__ == "__main__":
    main()

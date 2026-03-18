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
import json
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
            data["sandai_ketto"] = json.dumps(sandai, ensure_ascii=False)

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
                data[json_col] = json.dumps(vals, ensure_ascii=False)

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
            data["sandai_ketto_hanshoku"] = json.dumps(sandai, ensure_ascii=False)
            self._upsert("jvd_sanku", data)
            count += 1
        self.conn.commit()
        return count

    def import_ks(self, records: list[dict]):
        """KS（騎手マスタ）をインポート"""
        columns = self._get_table_columns("jvd_kishu")
        count = 0
        for rec in records:
            if rec.get("_record_type") != "KS":
                continue
            data = self._filter_columns(rec, columns)
            self._upsert("jvd_kishu", data)
            count += 1
        self.conn.commit()
        return count

    def import_ch(self, records: list[dict]):
        """CH（調教師マスタ）をインポート"""
        columns = self._get_table_columns("jvd_chokyoshi")
        count = 0
        for rec in records:
            if rec.get("_record_type") != "CH":
                continue
            data = self._filter_columns(rec, columns)
            self._upsert("jvd_chokyoshi", data)
            count += 1
        self.conn.commit()
        return count

    def import_br(self, records: list[dict]):
        """BR（生産者マスタ）をインポート"""
        columns = self._get_table_columns("jvd_seisansha")
        count = 0
        for rec in records:
            if rec.get("_record_type") != "BR":
                continue
            data = self._filter_columns(rec, columns)
            # 成績をJSON化（2回分）
            seiseki = []
            for i in range(1, 3):
                entry = {
                    "nendo": rec.get(f"seiseki_{i}_nendo", ""),
                    "hon_shokin": rec.get(f"seiseki_{i}_hon_shokin", ""),
                    "fuka_shokin": rec.get(f"seiseki_{i}_fuka_shokin", ""),
                    "chakukaisu": [
                        rec.get(f"seiseki_{i}_chaku_{j}", "0") for j in range(1, 6)
                    ] + [
                        rec.get(f"seiseki_{i}_chaku_gai", "0")
                    ],
                }
                seiseki.append(entry)
            data["seiseki"] = json.dumps(seiseki, ensure_ascii=False)
            self._upsert("jvd_seisansha", data)
            count += 1
        self.conn.commit()
        return count

    def import_bn(self, records: list[dict]):
        """BN（馬主マスタ）をインポート"""
        columns = self._get_table_columns("jvd_banushi")
        count = 0
        for rec in records:
            if rec.get("_record_type") != "BN":
                continue
            data = self._filter_columns(rec, columns)
            # 成績をJSON化（BR と同構造、2回分）
            seiseki = []
            for i in range(1, 3):
                entry = {
                    "nendo": rec.get(f"seiseki_{i}_nendo", ""),
                    "hon_shokin": rec.get(f"seiseki_{i}_hon_shokin", ""),
                    "fuka_shokin": rec.get(f"seiseki_{i}_fuka_shokin", ""),
                    "chakukaisu": [
                        rec.get(f"seiseki_{i}_chaku_{j}", "0") for j in range(1, 7)
                    ],
                }
                entry["chakukaisu"][5] = rec.get(f"seiseki_{i}_chaku_gai", "0")
                seiseki.append(entry)
            data["seiseki"] = json.dumps(seiseki, ensure_ascii=False)
            self._upsert("jvd_banushi", data)
            count += 1
        self.conn.commit()
        return count

    def import_hr(self, records: list[dict]):
        """HR（払戻）をインポート"""
        columns = self._get_table_columns("jvd_haraimodoshi")
        count = 0
        for rec in records:
            if rec.get("_record_type") != "HR":
                continue
            data = self._filter_columns(rec, columns)
            # 各券種の払戻をJSON化
            payoff_defs = [
                ("tansho", 3, "umaban"),
                ("fukusho", 5, "umaban"),
                ("wakuren", 3, "kumiban"),
                ("umaren", 3, "kumiban"),
                ("wide", 7, "kumiban"),
                ("umatan", 6, "kumiban"),
                ("sanrenpuku", 3, "kumiban"),
                ("sanrentan", 6, "kumiban"),
            ]
            for kenshu, max_n, ban_field in payoff_defs:
                entries = []
                for i in range(1, max_n + 1):
                    ban = rec.get(f"{kenshu}_{ban_field}_{i}", "").strip()
                    kin = rec.get(f"{kenshu}_haraimodoshi_{i}", "").strip()
                    ninki = rec.get(f"{kenshu}_ninki_{i}", "").strip()
                    if ban and ban != "0" * len(ban):
                        entries.append({
                            ban_field: ban,
                            "haraimodoshi_kin": kin,
                            "ninki": ninki,
                        })
                data[kenshu] = json.dumps(entries, ensure_ascii=False) if entries else None
            self._upsert("jvd_haraimodoshi", data)
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
        kt_dir = Path(f"{TFJV_DIR}/KT_DATA")
        if not kt_dir.is_dir():
            self.logger.warning(f"HN: {kt_dir} が見つからないためスキップ")
            return 0
        files = sorted(kt_dir.glob("KT2*.DAT"))
        if not files:
            self.logger.warning("HN: KT2*.DAT が見つからないためスキップ")
            return 0
        hn_recs = []
        for fpath in files:
            hn_recs.extend(parse_file(str(fpath), {"HN"}))
        n = self.import_hn(hn_recs)
        self.logger.info(f"HN（繁殖馬マスタ）: {n:,}件 インポート完了")
        return n

    def import_ks_all(self) -> int:
        """KS（騎手マスタ）を TFJ_KISI.DAT からインポート"""
        self.logger.info("KS（騎手マスタ）インポート開始...")
        try:
            recs = parse_file(f"{TFJV_DIR}/TFJ_KISI.DAT", {"KS"})
        except FileNotFoundError:
            self.logger.warning("KS: TFJ_KISI.DAT が見つからないためスキップ")
            return 0
        n = self.import_ks(recs)
        self.logger.info(f"KS（騎手マスタ）: {n:,}件 インポート完了")
        return n

    def import_ch_all(self) -> int:
        """CH（調教師マスタ）を TFJ_CHOK.DAT からインポート"""
        self.logger.info("CH（調教師マスタ）インポート開始...")
        try:
            recs = parse_file(f"{TFJV_DIR}/TFJ_CHOK.DAT", {"CH"})
        except FileNotFoundError:
            self.logger.warning("CH: TFJ_CHOK.DAT が見つからないためスキップ")
            return 0
        n = self.import_ch(recs)
        self.logger.info(f"CH（調教師マスタ）: {n:,}件 インポート完了")
        return n

    def import_br_all(self) -> int:
        """BR（生産者マスタ）を BR_DATA からインポート"""
        self.logger.info("BR（生産者マスタ）インポート開始...")
        br_dir = Path(f"{TFJV_DIR}/BR_DATA")
        if not br_dir.is_dir():
            self.logger.warning(f"BR: {br_dir} が見つからないためスキップ")
            return 0
        recs = []
        files = sorted(br_dir.glob("TFJ_BR*.DAT"))
        if not files:
            self.logger.warning("BR: TFJ_BR*.DAT が見つからないためスキップ")
            return 0
        for fpath in files:
            recs.extend(parse_file(str(fpath), {"BR"}))
        n = self.import_br(recs)
        self.logger.info(f"BR（生産者マスタ）: {n:,}件 インポート完了")
        return n

    def import_bn_all(self) -> int:
        """BN（馬主マスタ）を OW_DATA からインポート"""
        self.logger.info("BN（馬主マスタ）インポート開始...")
        ow_dir = Path(f"{TFJV_DIR}/OW_DATA")
        if not ow_dir.is_dir():
            self.logger.warning(f"BN: {ow_dir} が見つからないためスキップ")
            return 0
        recs = []
        files = sorted(ow_dir.glob("TFJ_OW*.DAT"))
        if not files:
            self.logger.warning("BN: TFJ_OW*.DAT が見つからないためスキップ")
            return 0
        for fpath in files:
            recs.extend(parse_file(str(fpath), {"BN"}))
        n = self.import_bn(recs)
        self.logger.info(f"BN（馬主マスタ）: {n:,}件 インポート完了")
        return n

    def import_masters(self, skip_hn: bool = False) -> dict:
        """全マスタデータ（HN/KS/CH/BR/BN）を一括インポート"""
        totals = {}
        if not skip_hn:
            totals["HN"] = self.import_hn_all()
        totals["KS"] = self.import_ks_all()
        totals["CH"] = self.import_ch_all()
        totals["BR"] = self.import_br_all()
        totals["BN"] = self.import_bn_all()
        return totals

    def import_all_years(self, years: list[int], resume: bool = False) -> dict:
        """年リストを順にインポートする。エラーは年単位でスキップして継続する"""
        total_start = time.time()
        success_years = []
        failed_years = []
        skipped_years = []
        grand_total = {"RA": 0, "SE": 0, "UM": 0, "SK": 0, "HR": 0}

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

            # HR（払戻）
            self.logger.info(f"HR（払戻）パース中... [{year}]")
            try:
                hr_recs = parse_directory(f"{TFJV_DIR}/SE_DATA", "SH*.DAT", {"HR"}, year=year)
                counts["HR"] = self.import_hr(hr_recs)
                self.logger.info(f"  HR: {counts['HR']:,}件")
            except FileNotFoundError:
                self.logger.warning(f"  HR: SE_DATA/{year} フォルダなし — スキップ")
                counts["HR"] = 0

            elapsed = time.time() - t0
            self.logger.info(
                f"{year}年 完了（{elapsed:.1f}秒）"
                f" RA={counts.get('RA', 0):,} SE={counts.get('SE', 0):,}"
                f" UM={counts.get('UM', 0):,} SK={counts.get('SK', 0):,}"
                f" HR={counts.get('HR', 0):,}"
            )
            return counts

        except Exception as e:
            elapsed = time.time() - t0
            self.logger.error(f"{year}年 インポート失敗（{elapsed:.1f}秒）: {e}", exc_info=True)
            return None

    def _log_summary(self):
        """各テーブルのレコード数をログ出力"""
        tables = [
            "jvd_race", "jvd_race_uma", "jvd_uma", "jvd_sanku", "jvd_hanshoku",
            "jvd_kishu", "jvd_chokyoshi", "jvd_seisansha", "jvd_banushi", "jvd_haraimodoshi",
        ]
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
                        help="インポート済み年をスキップ（jvd_race のレコード有無で判定）。マスタデータもスキップされます")
    parser.add_argument("--skip-hn", action="store_true",
                        help="HN（繁殖馬マスタ）インポートをスキップ（再実行時用）")
    parser.add_argument("--skip-masters", action="store_true",
                        help="マスタデータ（HN/KS/CH/BR/BN）インポートをスキップ")
    parser.add_argument("--masters-only", action="store_true",
                        help="マスタデータ（HN/KS/CH/BR/BN）をインポート（年別データはスキップ、--skip-hnでHN除外）")
    parser.add_argument("--db", type=str, default=DB_PATH, help="SQLiteデータベースパス")
    parser.add_argument("--log-file", type=str, default=default_log, help="ログファイルパス")

    args = parser.parse_args()

    # バリデーション
    if args.all_years and (args.start_year or args.end_year):
        parser.error("--all-years と --start-year/--end-year は同時指定できません")
    if args.masters_only and (args.year or args.all_years or args.start_year or args.end_year):
        parser.error("--masters-only と年指定オプション（--year/--all-years/--start-year/--end-year）は同時指定できません")
    if args.masters_only and args.skip_masters:
        parser.error("--masters-only と --skip-masters は同時指定できません")
    if args.masters_only and args.resume:
        parser.error("--masters-only と --resume は同時指定できません")

    # ロギング初期化
    logger = setup_logging(args.log_file)
    logger.info(f"ログファイル: {os.path.abspath(args.log_file)}")
    logger.info(f"DB: {os.path.abspath(args.db)}")

    # インポーター起動
    importer = JVDataImporter(db_path=args.db)
    importer.connect()
    importer.init_schema()

    if args.masters_only:
        # マスタのみモード（KS/CH/BR/BN + HN）
        importer.import_masters(skip_hn=args.skip_hn)
        importer._log_summary()

    elif args.all_years or args.start_year or args.end_year:
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

        if args.skip_masters or args.resume:
            logger.info(f"{'--skip-masters' if args.skip_masters else '--resume'}: マスタデータインポートをスキップ")
        else:
            importer.import_masters(skip_hn=args.skip_hn)

        importer.import_all_years(years, resume=args.resume)

    else:
        # 単年モード（従来互換）
        year = args.year or 2024
        if not args.skip_masters:
            importer.import_masters(skip_hn=args.skip_hn)
        importer.import_year(year=year)
        importer._log_summary()

    importer.close()
    logger.info("処理終了")


if __name__ == "__main__":
    main()

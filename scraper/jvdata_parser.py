"""
JV-Data バイナリパーサー

JRA-VAN Data Lab の固定長バイナリレコードを Python 辞書に変換する。
仕様書: docs/vdata_spec/JV-Data4901.xlsx (Ver.4.9.0.1)

データソース: D:\TFJV\*_DATA フォルダ
エンコーディング: Shift-JIS (cp932)
レコード区切り: CRLF (0x0d0a)
"""

import json
import os
from pathlib import Path
from typing import Optional


# ============================================================
# フィールド定義
# 仕様書「フォーマット」シートに基づく (name, byte_length)
# ※ 位置は累積計算で自動決定
# ============================================================

# --- RA: レース詳細 (仕様書セクション2, Row 74-140) ---
RA_FIELDS = [
    ("record_shubetsu_id", 2),
    ("data_kubun", 1),
    ("data_sakusei_ymd", 8),
    ("kaisai_nen", 4),
    ("kaisai_tsukihi", 4),
    ("keibajo_code", 2),
    ("kaisai_kai", 2),
    ("kaisai_nichime", 2),
    ("race_bango", 2),
    ("youbi_code", 1),
    ("tokubetsu_kyoso_bango", 4),
    ("kyoso_mei_hondai", 60),
    ("kyoso_mei_fukudai", 60),
    ("kyoso_mei_kakko", 60),
    ("kyoso_mei_hondai_eu", 120),
    ("kyoso_mei_fukudai_eu", 120),
    ("kyoso_mei_kakko_eu", 120),
    ("kyoso_mei_ryakusho10", 20),
    ("kyoso_mei_ryakusho6", 12),
    ("kyoso_mei_ryakusho3", 6),
    ("kyoso_mei_kubun", 1),
    ("jusho_kaiji", 3),
    ("grade_code", 1),
    ("henko_mae_grade_code", 1),
    ("kyoso_shubetsu_code", 2),
    ("kyoso_kigo_code", 3),
    ("juryo_shubetsu_code", 1),
    ("kyoso_joken_code_2sai", 3),
    ("kyoso_joken_code_3sai", 3),
    ("kyoso_joken_code_4sai", 3),
    ("kyoso_joken_code_5sai_ijo", 3),
    ("kyoso_joken_code_saijakuinen", 3),
    ("kyoso_joken_meisho", 60),
    ("kyori", 4),
    ("henko_mae_kyori", 4),
    ("track_code", 2),
    ("henko_mae_track_code", 2),
    ("course_kubun", 2),
    ("henko_mae_course_kubun", 2),
    # 繰返フィールド: 賞金
    ("hon_shokin_1", 8), ("hon_shokin_2", 8), ("hon_shokin_3", 8),
    ("hon_shokin_4", 8), ("hon_shokin_5", 8), ("hon_shokin_6", 8), ("hon_shokin_7", 8),
    ("henko_mae_hon_shokin_1", 8), ("henko_mae_hon_shokin_2", 8),
    ("henko_mae_hon_shokin_3", 8), ("henko_mae_hon_shokin_4", 8), ("henko_mae_hon_shokin_5", 8),
    ("fuka_shokin_1", 8), ("fuka_shokin_2", 8), ("fuka_shokin_3", 8),
    ("fuka_shokin_4", 8), ("fuka_shokin_5", 8),
    ("henko_mae_fuka_shokin_1", 8), ("henko_mae_fuka_shokin_2", 8), ("henko_mae_fuka_shokin_3", 8),
    ("hasso_jikoku", 4),
    ("henko_mae_hasso_jikoku", 4),
    ("toroku_tosu", 2),
    ("shusso_tosu", 2),
    ("nyusen_tosu", 2),
    ("tenko_code", 1),
    ("shiba_baba_jotai_code", 1),
    ("dirt_baba_jotai_code", 1),
    # ラップタイム 25回分
    ("lap_time_01", 3), ("lap_time_02", 3), ("lap_time_03", 3), ("lap_time_04", 3), ("lap_time_05", 3),
    ("lap_time_06", 3), ("lap_time_07", 3), ("lap_time_08", 3), ("lap_time_09", 3), ("lap_time_10", 3),
    ("lap_time_11", 3), ("lap_time_12", 3), ("lap_time_13", 3), ("lap_time_14", 3), ("lap_time_15", 3),
    ("lap_time_16", 3), ("lap_time_17", 3), ("lap_time_18", 3), ("lap_time_19", 3), ("lap_time_20", 3),
    ("lap_time_21", 3), ("lap_time_22", 3), ("lap_time_23", 3), ("lap_time_24", 3), ("lap_time_25", 3),
    ("shogai_mile_time", 4),
    ("mae_3f", 3),
    ("mae_4f", 3),
    ("ato_3f", 3),
    ("ato_4f", 3),
    # コーナー通過順位 4回分 (各 1+1+70=72B)
    ("corner_1_corner", 1), ("corner_1_shukai", 1), ("corner_1_juni", 70),
    ("corner_2_corner", 1), ("corner_2_shukai", 1), ("corner_2_juni", 70),
    ("corner_3_corner", 1), ("corner_3_shukai", 1), ("corner_3_juni", 70),
    ("corner_4_corner", 1), ("corner_4_shukai", 1), ("corner_4_juni", 70),
    ("record_koshin_kubun", 1),
]

# --- SE: 馬毎レース情報 (仕様書セクション3, Row 141-215) ---
SE_FIELDS = [
    ("record_shubetsu_id", 2),
    ("data_kubun", 1),
    ("data_sakusei_ymd", 8),
    ("kaisai_nen", 4),
    ("kaisai_tsukihi", 4),
    ("keibajo_code", 2),
    ("kaisai_kai", 2),
    ("kaisai_nichime", 2),
    ("race_bango", 2),
    ("wakuban", 1),
    ("umaban", 2),
    ("ketto_toroku_bango", 10),
    ("bamei", 36),
    ("uma_kigo_code", 2),
    ("seibetsu_code", 1),
    ("hinshu_code", 1),
    ("keiro_code", 2),
    ("barei", 2),
    ("tozai_shozoku_code", 1),
    ("chokyoshi_code", 5),
    ("chokyoshi_mei_ryakusho", 8),
    ("banushi_code", 6),
    ("banushi_mei", 64),
    ("fukushoku_hyoji", 60),
    ("_yobi_1", 60),
    ("futan_juryo", 3),
    ("henko_mae_futan_juryo", 3),
    ("blinker_shiyou_kubun", 1),
    ("_yobi_2", 1),
    ("kishu_code", 5),
    ("henko_mae_kishu_code", 5),
    ("kishu_mei_ryakusho", 8),
    ("henko_mae_kishu_mei_ryakusho", 8),
    ("kishu_minarai_code", 1),
    ("henko_mae_kishu_minarai_code", 1),
    ("bataiju", 3),
    ("zogen_fugo", 1),
    ("zogen_sa", 3),
    ("ijo_kubun_code", 1),
    ("nyusen_juni", 2),
    ("kakutei_chakujun", 2),
    ("dochaku_kubun", 1),
    ("dochaku_tosu", 1),
    ("soha_time", 4),
    ("chakusa_code", 3),
    ("chakusa_code_plus", 3),
    ("chakusa_code_plus2", 3),
    ("corner1_juni", 2),
    ("corner2_juni", 2),
    ("corner3_juni", 2),
    ("corner4_juni", 2),
    ("tansho_odds", 4),
    ("tansho_ninki_jun", 2),
    ("kakutoku_hon_shokin", 8),
    ("kakutoku_fuka_shokin", 8),
    ("_yobi_3", 3),
    ("_yobi_4", 3),
    ("ato_4f_time", 3),
    ("ato_3f_time", 3),
    # 1着馬(相手馬)情報 3頭分 (各 10+36=46B)
    ("aite_ketto_toroku_bango_1", 10), ("aite_bamei_1", 36),
    ("aite_ketto_toroku_bango_2", 10), ("aite_bamei_2", 36),
    ("aite_ketto_toroku_bango_3", 10), ("aite_bamei_3", 36),
    ("time_sa", 4),
    ("record_koshin_kubun", 1),
    ("mining_kubun", 1),
    ("mining_yoso_soha_time", 5),
    ("mining_yoso_gosa_plus", 4),
    ("mining_yoso_gosa_minus", 4),
    ("mining_yoso_juni", 2),
    ("kyakushitsu_hantei", 1),
]

# --- UM: 競走馬マスタ (仕様書セクション13, Row 544-617) ---
UM_FIELDS = [
    ("record_shubetsu_id", 2),
    ("data_kubun", 1),
    ("data_sakusei_ymd", 8),
    ("ketto_toroku_bango", 10),
    ("kyosouma_massho_kubun", 1),
    ("kyosouma_toroku_ymd", 8),
    ("kyosouma_massho_ymd", 8),
    ("seinengappi", 8),
    ("bamei", 36),
    ("bamei_kana", 36),
    ("bamei_eiji", 60),
    ("jra_shisetsu_zaikyu_flag", 1),
    ("_yobi_1", 19),
    ("uma_kigo_code", 2),
    ("seibetsu_code", 1),
    ("hinshu_code", 1),
    ("keiro_code", 2),
    # 3代血統情報 14頭分 (各 10+36=46B)
    ("ketto_hanshoku_bango_01", 10), ("ketto_bamei_01", 36),  # 父
    ("ketto_hanshoku_bango_02", 10), ("ketto_bamei_02", 36),  # 母
    ("ketto_hanshoku_bango_03", 10), ("ketto_bamei_03", 36),  # 父父
    ("ketto_hanshoku_bango_04", 10), ("ketto_bamei_04", 36),  # 父母
    ("ketto_hanshoku_bango_05", 10), ("ketto_bamei_05", 36),  # 母父
    ("ketto_hanshoku_bango_06", 10), ("ketto_bamei_06", 36),  # 母母
    ("ketto_hanshoku_bango_07", 10), ("ketto_bamei_07", 36),  # 父父父
    ("ketto_hanshoku_bango_08", 10), ("ketto_bamei_08", 36),  # 父父母
    ("ketto_hanshoku_bango_09", 10), ("ketto_bamei_09", 36),  # 父母父
    ("ketto_hanshoku_bango_10", 10), ("ketto_bamei_10", 36),  # 父母母
    ("ketto_hanshoku_bango_11", 10), ("ketto_bamei_11", 36),  # 母父父
    ("ketto_hanshoku_bango_12", 10), ("ketto_bamei_12", 36),  # 母父母
    ("ketto_hanshoku_bango_13", 10), ("ketto_bamei_13", 36),  # 母母父
    ("ketto_hanshoku_bango_14", 10), ("ketto_bamei_14", 36),  # 母母母
    ("tozai_shozoku_code", 1),
    ("chokyoshi_code", 5),
    ("chokyoshi_mei_ryakusho", 8),
    ("shotai_chiiki_mei", 20),
    ("seisansha_code", 8),
    ("seisansha_mei", 72),
    ("sanchi_mei", 20),
    ("banushi_code", 6),
    ("banushi_mei", 64),
    ("heichi_hon_shokin_ruikei", 9),
    ("shogai_hon_shokin_ruikei", 9),
    ("heichi_fuka_shokin_ruikei", 9),
    ("shogai_fuka_shokin_ruikei", 9),
    ("heichi_shutoku_shokin_ruikei", 9),
    ("shogai_shutoku_shokin_ruikei", 9),
    # 着回数: 総合6回 + 中央6回 = 36B
    ("sogo_chakukaisu_1chaku", 3), ("sogo_chakukaisu_2chaku", 3),
    ("sogo_chakukaisu_3chaku", 3), ("sogo_chakukaisu_4chaku", 3),
    ("sogo_chakukaisu_5chaku", 3), ("sogo_chakukaisu_chakugai", 3),
    ("chuo_chakukaisu_1chaku", 3), ("chuo_chakukaisu_2chaku", 3),
    ("chuo_chakukaisu_3chaku", 3), ("chuo_chakukaisu_4chaku", 3),
    ("chuo_chakukaisu_5chaku", 3), ("chuo_chakukaisu_chakugai", 3),
    # 馬場別着回数 (7種 x 6回 = 126B)
    ("shiba_choku_1", 3), ("shiba_choku_2", 3), ("shiba_choku_3", 3),
    ("shiba_choku_4", 3), ("shiba_choku_5", 3), ("shiba_choku_6", 3),
    ("shiba_migi_1", 3), ("shiba_migi_2", 3), ("shiba_migi_3", 3),
    ("shiba_migi_4", 3), ("shiba_migi_5", 3), ("shiba_migi_6", 3),
    ("shiba_hidari_1", 3), ("shiba_hidari_2", 3), ("shiba_hidari_3", 3),
    ("shiba_hidari_4", 3), ("shiba_hidari_5", 3), ("shiba_hidari_6", 3),
    ("dirt_choku_1", 3), ("dirt_choku_2", 3), ("dirt_choku_3", 3),
    ("dirt_choku_4", 3), ("dirt_choku_5", 3), ("dirt_choku_6", 3),
    ("dirt_migi_1", 3), ("dirt_migi_2", 3), ("dirt_migi_3", 3),
    ("dirt_migi_4", 3), ("dirt_migi_5", 3), ("dirt_migi_6", 3),
    ("dirt_hidari_1", 3), ("dirt_hidari_2", 3), ("dirt_hidari_3", 3),
    ("dirt_hidari_4", 3), ("dirt_hidari_5", 3), ("dirt_hidari_6", 3),
    ("shogai_1", 3), ("shogai_2", 3), ("shogai_3", 3),
    ("shogai_4", 3), ("shogai_5", 3), ("shogai_6", 3),
    # 馬場状態別着回数 (12種 x 6回 = 216B)
    ("shiba_ryo_1", 3), ("shiba_ryo_2", 3), ("shiba_ryo_3", 3),
    ("shiba_ryo_4", 3), ("shiba_ryo_5", 3), ("shiba_ryo_6", 3),
    ("shiba_yaya_1", 3), ("shiba_yaya_2", 3), ("shiba_yaya_3", 3),
    ("shiba_yaya_4", 3), ("shiba_yaya_5", 3), ("shiba_yaya_6", 3),
    ("shiba_omo_1", 3), ("shiba_omo_2", 3), ("shiba_omo_3", 3),
    ("shiba_omo_4", 3), ("shiba_omo_5", 3), ("shiba_omo_6", 3),
    ("shiba_fu_1", 3), ("shiba_fu_2", 3), ("shiba_fu_3", 3),
    ("shiba_fu_4", 3), ("shiba_fu_5", 3), ("shiba_fu_6", 3),
    ("dirt_ryo_1", 3), ("dirt_ryo_2", 3), ("dirt_ryo_3", 3),
    ("dirt_ryo_4", 3), ("dirt_ryo_5", 3), ("dirt_ryo_6", 3),
    ("dirt_yaya_1", 3), ("dirt_yaya_2", 3), ("dirt_yaya_3", 3),
    ("dirt_yaya_4", 3), ("dirt_yaya_5", 3), ("dirt_yaya_6", 3),
    ("dirt_omo_1", 3), ("dirt_omo_2", 3), ("dirt_omo_3", 3),
    ("dirt_omo_4", 3), ("dirt_omo_5", 3), ("dirt_omo_6", 3),
    ("dirt_fu_1", 3), ("dirt_fu_2", 3), ("dirt_fu_3", 3),
    ("dirt_fu_4", 3), ("dirt_fu_5", 3), ("dirt_fu_6", 3),
    ("shogai_ryo_1", 3), ("shogai_ryo_2", 3), ("shogai_ryo_3", 3),
    ("shogai_ryo_4", 3), ("shogai_ryo_5", 3), ("shogai_ryo_6", 3),
    ("shogai_yaya_1", 3), ("shogai_yaya_2", 3), ("shogai_yaya_3", 3),
    ("shogai_yaya_4", 3), ("shogai_yaya_5", 3), ("shogai_yaya_6", 3),
    ("shogai_omo_1", 3), ("shogai_omo_2", 3), ("shogai_omo_3", 3),
    ("shogai_omo_4", 3), ("shogai_omo_5", 3), ("shogai_omo_6", 3),
    ("shogai_fu_1", 3), ("shogai_fu_2", 3), ("shogai_fu_3", 3),
    ("shogai_fu_4", 3), ("shogai_fu_5", 3), ("shogai_fu_6", 3),
    # 距離別着回数 (6種 x 6回 = 108B)
    ("shiba_16ka_1", 3), ("shiba_16ka_2", 3), ("shiba_16ka_3", 3),
    ("shiba_16ka_4", 3), ("shiba_16ka_5", 3), ("shiba_16ka_6", 3),
    ("shiba_22ka_1", 3), ("shiba_22ka_2", 3), ("shiba_22ka_3", 3),
    ("shiba_22ka_4", 3), ("shiba_22ka_5", 3), ("shiba_22ka_6", 3),
    ("shiba_22cho_1", 3), ("shiba_22cho_2", 3), ("shiba_22cho_3", 3),
    ("shiba_22cho_4", 3), ("shiba_22cho_5", 3), ("shiba_22cho_6", 3),
    ("dirt_16ka_1", 3), ("dirt_16ka_2", 3), ("dirt_16ka_3", 3),
    ("dirt_16ka_4", 3), ("dirt_16ka_5", 3), ("dirt_16ka_6", 3),
    ("dirt_22ka_1", 3), ("dirt_22ka_2", 3), ("dirt_22ka_3", 3),
    ("dirt_22ka_4", 3), ("dirt_22ka_5", 3), ("dirt_22ka_6", 3),
    ("dirt_22cho_1", 3), ("dirt_22cho_2", 3), ("dirt_22cho_3", 3),
    ("dirt_22cho_4", 3), ("dirt_22cho_5", 3), ("dirt_22cho_6", 3),
    # 脚質傾向 (4回分 x 3B = 12B)
    ("kyakushitsu_keiko_1", 3), ("kyakushitsu_keiko_2", 3),
    ("kyakushitsu_keiko_3", 3), ("kyakushitsu_keiko_4", 3),
    ("toroku_race_su", 3),
]

# --- HN: 繁殖馬マスタ (仕様書セクション18, Row 821-844) ---
HN_FIELDS = [
    ("record_shubetsu_id", 2),
    ("data_kubun", 1),
    ("data_sakusei_ymd", 8),
    ("hanshoku_toroku_bango", 10),
    ("_yobi_1", 8),
    ("ketto_toroku_bango", 10),
    ("_yobi_2", 1),
    ("bamei", 36),
    ("bamei_kana", 40),
    ("bamei_eiji", 80),
    ("seinen", 4),
    ("seibetsu_code", 1),
    ("hinshu_code", 1),
    ("keiro_code", 2),
    ("hanshokuba_mochikomi_kubun", 1),
    ("yunyu_nen", 4),
    ("sanchi_mei", 20),
    ("chichiuma_hanshoku_toroku_bango", 10),
    ("hahauma_hanshoku_toroku_bango", 10),
]

# --- SK: 産駒マスタ (仕様書セクション19, Row 845-862) ---
SK_FIELDS = [
    ("record_shubetsu_id", 2),
    ("data_kubun", 1),
    ("data_sakusei_ymd", 8),
    ("ketto_toroku_bango", 10),
    ("seinengappi", 8),
    ("seibetsu_code", 1),
    ("hinshu_code", 1),
    ("keiro_code", 2),
    ("sanku_mochikomi_kubun", 1),
    ("yunyu_nen", 4),
    ("seisansha_code", 8),
    ("sanchi_mei", 20),
    # 3代血統 繁殖登録番号 14頭分 (各10B)
    ("sandai_hanshoku_01", 10), ("sandai_hanshoku_02", 10),
    ("sandai_hanshoku_03", 10), ("sandai_hanshoku_04", 10),
    ("sandai_hanshoku_05", 10), ("sandai_hanshoku_06", 10),
    ("sandai_hanshoku_07", 10), ("sandai_hanshoku_08", 10),
    ("sandai_hanshoku_09", 10), ("sandai_hanshoku_10", 10),
    ("sandai_hanshoku_11", 10), ("sandai_hanshoku_12", 10),
    ("sandai_hanshoku_13", 10), ("sandai_hanshoku_14", 10),
]

# レコード種別ID → フィールド定義のマッピング
RECORD_FIELDS = {
    "RA": RA_FIELDS,
    "SE": SE_FIELDS,
    "UM": UM_FIELDS,
    "HN": HN_FIELDS,
    "SK": SK_FIELDS,
}

# テキストフィールド（Shift-JIS → UTF-8 変換対象）
# それ以外は ASCII 数値として扱う
TEXT_FIELD_SUFFIXES = (
    "_hondai", "_fukudai", "_kakko", "_eu", "_ryakusho",
    "_meisho", "bamei", "_kana", "_eiji", "_mei",
    "_hyoji", "_juni",  # コーナー通過順位テキスト
    "shotai_chiiki_mei", "sanchi_mei",
)


def _is_text_field(name: str) -> bool:
    """テキストフィールドかどうかを判定"""
    if name.startswith("_yobi"):
        return False
    for suffix in TEXT_FIELD_SUFFIXES:
        if name.endswith(suffix) or suffix in name:
            return True
    return False


def _build_offset_map(fields: list[tuple[str, int]]) -> list[tuple[str, int, int]]:
    """フィールド定義から (name, offset, length) のリストを生成"""
    result = []
    offset = 0
    for name, length in fields:
        result.append((name, offset, length))
        offset += length
    return result


def _decode_field(raw: bytes, name: str) -> str:
    """バイト列をフィールド名に応じてデコード"""
    if _is_text_field(name):
        try:
            return raw.decode("cp932").strip()
        except UnicodeDecodeError:
            return raw.decode("cp932", errors="replace").strip()
    else:
        try:
            return raw.decode("ascii").strip()
        except UnicodeDecodeError:
            return raw.hex()


def parse_record(record: bytes) -> Optional[dict]:
    """1レコード（CRLF除去済みバイト列）をパースして辞書を返す"""
    if len(record) < 2:
        return None

    record_type = record[:2].decode("ascii", errors="replace")
    fields = RECORD_FIELDS.get(record_type)
    if fields is None:
        return None

    offset_map = _build_offset_map(fields)
    result = {"_record_type": record_type}

    for name, offset, length in offset_map:
        if name.startswith("_yobi"):
            continue
        if offset + length > len(record):
            result[name] = ""
            continue
        raw = record[offset:offset + length]
        result[name] = _decode_field(raw, name)

    return result


def parse_file(filepath: str, record_types: Optional[set[str]] = None) -> list[dict]:
    """DATファイルを読み込み、全レコードをパースして辞書リストを返す

    Args:
        filepath: DATファイルパス
        record_types: パース対象のレコード種別ID集合（Noneなら全て）
    """
    with open(filepath, "rb") as f:
        data = f.read()

    records = data.split(b"\r\n")
    results = []

    for rec in records:
        if len(rec) < 2:
            continue
        rt = rec[:2].decode("ascii", errors="replace")
        if record_types and rt not in record_types:
            continue
        parsed = parse_record(rec)
        if parsed:
            results.append(parsed)

    return results


def parse_directory(
    data_dir: str,
    file_pattern: str = "*.DAT",
    record_types: Optional[set[str]] = None,
    year: Optional[int] = None,
) -> list[dict]:
    """ディレクトリ内の全DATファイルをパース

    Args:
        data_dir: データディレクトリパス（例: D:/TFJV/SE_DATA）
        file_pattern: ファイルパターン
        record_types: パース対象のレコード種別ID集合
        year: 年指定（年サブフォルダがある場合）
    """
    base = Path(data_dir)
    if year:
        base = base / str(year)

    if not base.exists():
        raise FileNotFoundError(f"ディレクトリが見つかりません: {base}")

    results = []
    for fpath in sorted(base.glob(file_pattern)):
        if fpath.is_file():
            parsed = parse_file(str(fpath), record_types)
            results.extend(parsed)

    return results


# ============================================================
# テスト用メイン
# ============================================================
if __name__ == "__main__":
    import sys

    # SE_DATA/2024 の RA レコードを試しにパース
    test_dir = "D:/TFJV/SE_DATA"
    print("=== RA（レース詳細）パーステスト ===")
    ra_records = parse_directory(test_dir, "SR*.DAT", {"RA"}, year=2024)
    print(f"RAレコード数: {len(ra_records)}")
    if ra_records:
        r = ra_records[0]
        print(f"  開催: {r['kaisai_nen']}/{r['kaisai_tsukihi']} "
              f"場={r['keibajo_code']} R{r['race_bango']}")
        print(f"  競走名: {r['kyoso_mei_hondai']}")
        print(f"  距離: {r['kyori']}m  トラック: {r['track_code']}")
        print(f"  出走頭数: {r['shusso_tosu']}  天候: {r['tenko_code']}")

    print()
    print("=== SE（馬毎レース情報）パーステスト ===")
    se_records = parse_directory(test_dir, "SU*.DAT", {"SE"}, year=2024)
    print(f"SEレコード数: {len(se_records)}")
    if se_records:
        s = se_records[0]
        print(f"  {s['kaisai_nen']}/{s['kaisai_tsukihi']} R{s['race_bango']}")
        print(f"  馬番{s['umaban']} {s['bamei']}（{s['ketto_toroku_bango']}）")
        print(f"  騎手: {s['kishu_mei_ryakusho']}  着順: {s['kakutei_chakujun']}")
        print(f"  走破タイム: {s['soha_time']}  後3F: {s['ato_3f_time']}")
        print(f"  単勝: {s['tansho_odds']}  人気: {s['tansho_ninki_jun']}")

    print()
    print("=== UM（競走馬マスタ）パーステスト ===")
    um_records = parse_directory("D:/TFJV/UM_DATA", "UM*.DAT", {"UM"}, year=2024)
    print(f"UMレコード数: {len(um_records)}")
    if um_records:
        u = um_records[0]
        print(f"  {u['bamei']}（{u['ketto_toroku_bango']}）")
        print(f"  生年月日: {u['seinengappi']}  性別: {u['seibetsu_code']}")
        print(f"  父: {u['ketto_bamei_01']}  母: {u['ketto_bamei_02']}")
        print(f"  母父: {u['ketto_bamei_05']}  父父: {u['ketto_bamei_03']}")
        print(f"  調教師: {u['chokyoshi_mei_ryakusho']}  馬主: {u['banushi_mei']}")

    print()
    print("=== HN（繁殖馬マスタ）パーステスト ===")
    hn_records = parse_directory("D:/TFJV/KT_DATA", "KT2*.DAT", {"HN"}, year=None)
    # KT_DATAは年サブフォルダなしの直下ファイル
    if not hn_records:
        hn_records = parse_file("D:/TFJV/KT_DATA/KT2_24.DAT", {"HN"})
    print(f"HNレコード数: {len(hn_records)}")
    if hn_records:
        h = hn_records[0]
        print(f"  {h['bamei']}（{h['hanshoku_toroku_bango']}）")
        print(f"  父: {h['chichiuma_hanshoku_toroku_bango']}")
        print(f"  母: {h['hahauma_hanshoku_toroku_bango']}")

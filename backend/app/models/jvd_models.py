"""
JV-Data 仕様書準拠 SQLAlchemy ORM モデル

仕様書: docs/vdata_spec/JV-Data4901.xlsx (Ver.4.9.0.1)
スキーマ: backend/jvdata_schema.sql
"""

from sqlalchemy import Column, String, Text
from sqlalchemy.orm import DeclarativeBase


class JVDBase(DeclarativeBase):
    """JV-Dataテーブル用ベースクラス"""
    pass


class JVDRace(JVDBase):
    """RA（レース詳細）— 仕様書セクション2"""
    __tablename__ = "jvd_race"

    # ヘッダ
    record_shubetsu_id = Column(String, nullable=False)
    data_kubun = Column(String, nullable=False)
    data_sakusei_ymd = Column(String, nullable=False)

    # キー項目
    kaisai_nen = Column(String, primary_key=True)
    kaisai_tsukihi = Column(String, primary_key=True)
    keibajo_code = Column(String, primary_key=True)
    kaisai_kai = Column(String, primary_key=True)
    kaisai_nichime = Column(String, primary_key=True)
    race_bango = Column(String, primary_key=True)

    # レース情報
    youbi_code = Column(String)
    tokubetsu_kyoso_bango = Column(String)
    kyoso_mei_hondai = Column(String)
    kyoso_mei_fukudai = Column(String)
    kyoso_mei_kakko = Column(String)
    kyoso_mei_hondai_欧 = Column(String)
    kyoso_mei_fukudai_欧 = Column(String)
    kyoso_mei_kakko_欧 = Column(String)
    kyoso_mei_ryakusho10 = Column(String)
    kyoso_mei_ryakusho6 = Column(String)
    kyoso_mei_ryakusho3 = Column(String)
    kyoso_mei_kubun = Column(String)
    jusho_kaiji = Column(String)
    grade_code = Column(String)
    henko_mae_grade_code = Column(String)
    kyoso_shubetsu_code = Column(String)
    kyoso_kigo_code = Column(String)
    juryo_shubetsu_code = Column(String)
    kyoso_joken_code_2sai = Column(String)
    kyoso_joken_code_3sai = Column(String)
    kyoso_joken_code_4sai = Column(String)
    kyoso_joken_code_5sai_ijo = Column(String)
    kyoso_joken_code_saijakuinen = Column(String)
    kyoso_joken_meisho = Column(String)
    kyori = Column(String)
    henko_mae_kyori = Column(String)
    track_code = Column(String)
    henko_mae_track_code = Column(String)
    course_kubun = Column(String)
    henko_mae_course_kubun = Column(String)
    hon_shokin = Column(Text)
    henko_mae_hon_shokin = Column(Text)
    fuka_shokin = Column(Text)
    henko_mae_fuka_shokin = Column(Text)
    hasso_jikoku = Column(String)
    henko_mae_hasso_jikoku = Column(String)
    toroku_tosu = Column(String)
    shusso_tosu = Column(String)
    nyusen_tosu = Column(String)
    tenko_code = Column(String)
    shiba_baba_jotai_code = Column(String)
    dirt_baba_jotai_code = Column(String)
    lap_time = Column(Text)
    shogai_mile_time = Column(String)
    mae_3f = Column(String)
    mae_4f = Column(String)
    ato_3f = Column(String)
    ato_4f = Column(String)
    corner_tsuka_juni = Column(Text)
    record_koshin_kubun = Column(String)


class JVDRaceUma(JVDBase):
    """SE（馬毎レース情報）— 仕様書セクション3"""
    __tablename__ = "jvd_race_uma"

    # ヘッダ
    record_shubetsu_id = Column(String, nullable=False)
    data_kubun = Column(String, nullable=False)
    data_sakusei_ymd = Column(String, nullable=False)

    # キー項目
    kaisai_nen = Column(String, primary_key=True)
    kaisai_tsukihi = Column(String, primary_key=True)
    keibajo_code = Column(String, primary_key=True)
    kaisai_kai = Column(String, primary_key=True)
    kaisai_nichime = Column(String, primary_key=True)
    race_bango = Column(String, primary_key=True)
    umaban = Column(String, primary_key=True)
    ketto_toroku_bango = Column(String, primary_key=True)

    # 馬情報
    wakuban = Column(String)
    bamei = Column(String)
    uma_kigo_code = Column(String)
    seibetsu_code = Column(String)
    hinshu_code = Column(String)
    keiro_code = Column(String)
    barei = Column(String)
    tozai_shozoku_code = Column(String)
    chokyoshi_code = Column(String)
    chokyoshi_mei_ryakusho = Column(String)
    banushi_code = Column(String)
    banushi_mei = Column(String)
    fukushoku_hyoji = Column(String)

    # 斤量・騎手
    futan_juryo = Column(String)
    henko_mae_futan_juryo = Column(String)
    blinker_shiyou_kubun = Column(String)
    kishu_code = Column(String)
    henko_mae_kishu_code = Column(String)
    kishu_mei_ryakusho = Column(String)
    henko_mae_kishu_mei_ryakusho = Column(String)
    kishu_minarai_code = Column(String)
    henko_mae_kishu_minarai_code = Column(String)

    # 馬体重
    bataiju = Column(String)
    zogen_fugo = Column(String)
    zogen_sa = Column(String)

    # 着順・結果
    ijo_kubun_code = Column(String)
    nyusen_juni = Column(String)
    kakutei_chakujun = Column(String)
    dochaku_kubun = Column(String)
    dochaku_tosu = Column(String)
    soha_time = Column(String)
    chakusa_code = Column(String)
    chakusa_code_plus = Column(String)
    chakusa_code_plus2 = Column(String)

    # コーナー通過順位
    corner1_juni = Column(String)
    corner2_juni = Column(String)
    corner3_juni = Column(String)
    corner4_juni = Column(String)

    # オッズ・人気
    tansho_odds = Column(String)
    tansho_ninki_jun = Column(String)

    # 賞金
    kakutoku_hon_shokin = Column(String)
    kakutoku_fuka_shokin = Column(String)

    # タイム
    ato_4f_time = Column(String)
    ato_3f_time = Column(String)

    # 相手馬情報
    aite_uma = Column(Text)

    # その他
    time_sa = Column(String)
    record_koshin_kubun = Column(String)
    mining_kubun = Column(String)
    mining_yoso_soha_time = Column(String)
    mining_yoso_gosa_plus = Column(String)
    mining_yoso_gosa_minus = Column(String)
    mining_yoso_juni = Column(String)
    kyakushitsu_hantei = Column(String)


class JVDUma(JVDBase):
    """UM（競走馬マスタ）— 仕様書セクション13"""
    __tablename__ = "jvd_uma"

    # ヘッダ
    record_shubetsu_id = Column(String, nullable=False)
    data_kubun = Column(String, nullable=False)
    data_sakusei_ymd = Column(String, nullable=False)

    # キー項目
    ketto_toroku_bango = Column(String, primary_key=True)

    # 基本情報
    kyosouma_massho_kubun = Column(String)
    kyosouma_toroku_ymd = Column(String)
    kyosouma_massho_ymd = Column(String)
    seinengappi = Column(String)
    bamei = Column(String)
    bamei_kana = Column(String)
    bamei_eiji = Column(String)
    jra_shisetsu_zaikyu_flag = Column(String)
    uma_kigo_code = Column(String)
    seibetsu_code = Column(String)
    hinshu_code = Column(String)
    keiro_code = Column(String)

    # 3代血統情報
    sandai_ketto = Column(Text)

    # 所属
    tozai_shozoku_code = Column(String)
    chokyoshi_code = Column(String)
    chokyoshi_mei_ryakusho = Column(String)
    shotai_chiiki_mei = Column(String)

    # 生産・馬主
    seisansha_code = Column(String)
    seisansha_mei = Column(String)
    sanchi_mei = Column(String)
    banushi_code = Column(String)
    banushi_mei = Column(String)

    # 賞金累計
    heichi_hon_shokin_ruikei = Column(String)
    shogai_hon_shokin_ruikei = Column(String)
    heichi_fuka_shokin_ruikei = Column(String)
    shogai_fuka_shokin_ruikei = Column(String)
    heichi_shutoku_shokin_ruikei = Column(String)
    shogai_shutoku_shokin_ruikei = Column(String)

    # 着回数
    sogo_chakukaisu = Column(Text)
    chuo_gokei_chakukaisu = Column(Text)
    baba_betsu_chakukaisu = Column(Text)
    baba_jotai_betsu_chakukaisu = Column(Text)
    kyori_betsu_chakukaisu = Column(Text)

    # 脚質傾向・登録レース数
    kyakushitsu_keiko = Column(Text)
    toroku_race_su = Column(String)


class JVDHanshoku(JVDBase):
    """HN（繁殖馬マスタ）— 仕様書セクション18"""
    __tablename__ = "jvd_hanshoku"

    # ヘッダ
    record_shubetsu_id = Column(String, nullable=False)
    data_kubun = Column(String, nullable=False)
    data_sakusei_ymd = Column(String, nullable=False)

    # キー項目
    hanshoku_toroku_bango = Column(String, primary_key=True)

    # 基本情報
    ketto_toroku_bango = Column(String)
    bamei = Column(String)
    bamei_kana = Column(String)
    bamei_eiji = Column(String)
    seinen = Column(String)
    seibetsu_code = Column(String)
    hinshu_code = Column(String)
    keiro_code = Column(String)
    hanshokuba_mochikomi_kubun = Column(String)
    yunyu_nen = Column(String)
    sanchi_mei = Column(String)

    # 血統リンク
    chichiuma_hanshoku_toroku_bango = Column(String)
    hahauma_hanshoku_toroku_bango = Column(String)


class JVDSanku(JVDBase):
    """SK（産駒マスタ）— 仕様書セクション19"""
    __tablename__ = "jvd_sanku"

    # ヘッダ
    record_shubetsu_id = Column(String, nullable=False)
    data_kubun = Column(String, nullable=False)
    data_sakusei_ymd = Column(String, nullable=False)

    # キー項目
    ketto_toroku_bango = Column(String, primary_key=True)

    # 基本情報
    seinengappi = Column(String)
    seibetsu_code = Column(String)
    hinshu_code = Column(String)
    keiro_code = Column(String)
    sanku_mochikomi_kubun = Column(String)
    yunyu_nen = Column(String)
    seisansha_code = Column(String)
    sanchi_mei = Column(String)

    # 3代血統
    sandai_ketto_hanshoku = Column(Text)


class JVDKishu(JVDBase):
    """KS（騎手マスタ）— 仕様書セクション14"""
    __tablename__ = "jvd_kishu"

    # ヘッダ
    record_shubetsu_id = Column(String, nullable=False)
    data_kubun = Column(String, nullable=False)
    data_sakusei_ymd = Column(String, nullable=False)

    # キー項目
    kishu_code = Column(String, primary_key=True)

    # 基本情報
    kishu_massho_kubun = Column(String)
    kishu_menkyo_kofu_ymd = Column(String)
    kishu_menkyo_massho_ymd = Column(String)
    seinengappi = Column(String)
    kishu_mei = Column(String)
    kishu_mei_kana = Column(String)
    kishu_mei_ryakusho = Column(String)
    kishu_mei_eiji = Column(String)
    seibetsu_kubun = Column(String)
    kijo_shikaku_code = Column(String)
    kishu_minarai_code = Column(String)
    kishu_tozai_shozoku_code = Column(String)
    shotai_chiiki_mei = Column(String)
    shozoku_chokyoshi_code = Column(String)
    shozoku_chokyoshi_mei_ryakusho = Column(String)

    # JSON
    hatsu_kijo = Column(Text)
    hatsu_shori = Column(Text)
    saikin_jusho_shori = Column(Text)
    seiseki = Column(Text)


class JVDChokyoshi(JVDBase):
    """CH（調教師マスタ）— 仕様書セクション15"""
    __tablename__ = "jvd_chokyoshi"

    # ヘッダ
    record_shubetsu_id = Column(String, nullable=False)
    data_kubun = Column(String, nullable=False)
    data_sakusei_ymd = Column(String, nullable=False)

    # キー項目
    chokyoshi_code = Column(String, primary_key=True)

    # 基本情報
    chokyoshi_massho_kubun = Column(String)
    chokyoshi_menkyo_kofu_ymd = Column(String)
    chokyoshi_menkyo_massho_ymd = Column(String)
    seinengappi = Column(String)
    chokyoshi_mei = Column(String)
    chokyoshi_mei_kana = Column(String)
    chokyoshi_mei_ryakusho = Column(String)
    chokyoshi_mei_eiji = Column(String)
    seibetsu_kubun = Column(String)
    chokyoshi_tozai_shozoku_code = Column(String)
    shotai_chiiki_mei = Column(String)

    # JSON
    saikin_jusho_shori = Column(Text)
    seiseki = Column(Text)


class JVDHaraimodoshi(JVDBase):
    """HR（払戻）— 仕様書セクション4"""
    __tablename__ = "jvd_haraimodoshi"

    # ヘッダ
    record_shubetsu_id = Column(String, nullable=False)
    data_kubun = Column(String, nullable=False)
    data_sakusei_ymd = Column(String, nullable=False)

    # キー項目
    kaisai_nen = Column(String, primary_key=True)
    kaisai_tsukihi = Column(String, primary_key=True)
    keibajo_code = Column(String, primary_key=True)
    kaisai_kai = Column(String, primary_key=True)
    kaisai_nichime = Column(String, primary_key=True)
    race_bango = Column(String, primary_key=True)

    # 払戻情報
    tansho = Column(Text)
    fukusho = Column(Text)
    wakuren = Column(Text)
    umaren = Column(Text)
    wide = Column(Text)
    umatan = Column(Text)
    sanrenpuku = Column(Text)
    sanrentan = Column(Text)


class JVDSeisansha(JVDBase):
    """BR（生産者マスタ）— 仕様書セクション16"""
    __tablename__ = "jvd_seisansha"

    # ヘッダ
    record_shubetsu_id = Column(String, nullable=False)
    data_kubun = Column(String, nullable=False)
    data_sakusei_ymd = Column(String, nullable=False)

    # キー項目
    seisansha_code = Column(String, primary_key=True)

    # 基本情報
    seisansha_mei_hojinkaku_ari = Column(String)
    seisansha_mei = Column(String)
    seisansha_mei_kana = Column(String)
    seisansha_mei_eiji = Column(String)
    seisansha_jusho = Column(String)

    # JSON
    seiseki = Column(Text)


class JVDBanushi(JVDBase):
    """BN（馬主マスタ）— 仕様書セクション17"""
    __tablename__ = "jvd_banushi"

    # ヘッダ
    record_shubetsu_id = Column(String, nullable=False)
    data_kubun = Column(String, nullable=False)
    data_sakusei_ymd = Column(String, nullable=False)

    # キー項目
    banushi_code = Column(String, primary_key=True)

    # 基本情報
    banushi_mei_hojinkaku_ari = Column(String)
    banushi_mei = Column(String)
    banushi_mei_kana = Column(String)
    banushi_mei_eiji = Column(String)
    fukushoku_hyoji = Column(String)

    # JSON
    seiseki = Column(Text)

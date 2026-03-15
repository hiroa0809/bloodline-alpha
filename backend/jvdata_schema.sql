-- ============================================================
-- JV-Data 仕様書準拠 SQLite スキーマ
-- 仕様書: docs/vdata_spec/JV-Data4901.xlsx (Ver.4.9.0.1)
--
-- 命名規則:
--   テーブル名: jvd_ + レコード種別の略称
--   カラム名:   仕様書項目名のローマ字スネークケース
--   キー:       仕様書の「○」マーク項目
--
-- 繰返項目の扱い:
--   少数(2-3回): _01, _02, _03 でフラット化
--   多数(6回以上の着回数等): JSON型で格納
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- 2. レース詳細 (RA) — 仕様書セクション2, Row 74-140
-- ============================================================
CREATE TABLE IF NOT EXISTS jvd_race (
    -- ヘッダ
    record_shubetsu_id  TEXT NOT NULL,       -- レコード種別ID (2B)
    data_kubun          TEXT NOT NULL,       -- データ区分 (1B)
    data_sakusei_ymd    TEXT NOT NULL,       -- データ作成年月日 (8B)

    -- キー項目 (○)
    kaisai_nen          TEXT NOT NULL,       -- 開催年 (4B)
    kaisai_tsukihi      TEXT NOT NULL,       -- 開催月日 (4B)
    keibajo_code        TEXT NOT NULL,       -- 競馬場コード (2B)
    kaisai_kai          TEXT NOT NULL,       -- 開催回[第N回] (2B)
    kaisai_nichime      TEXT NOT NULL,       -- 開催日目[N日目] (2B)
    race_bango          TEXT NOT NULL,       -- レース番号 (2B)

    -- レース情報
    youbi_code          TEXT,               -- 曜日コード (1B)
    tokubetsu_kyoso_bango TEXT,             -- 特別競走番号 (4B)
    kyoso_mei_hondai    TEXT,               -- 競走名本題 (60B)
    kyoso_mei_fukudai   TEXT,               -- 競走名副題 (60B)
    kyoso_mei_kakko     TEXT,               -- 競走名カッコ内 (60B)
    kyoso_mei_hondai_欧 TEXT,               -- 競走名本題欧字 (120B)
    kyoso_mei_fukudai_欧 TEXT,              -- 競走名副題欧字 (120B)
    kyoso_mei_kakko_欧  TEXT,               -- 競走名カッコ内欧字 (120B)
    kyoso_mei_ryakusho10 TEXT,              -- 競走名略称10文字 (20B)
    kyoso_mei_ryakusho6 TEXT,               -- 競走名略称6文字 (12B)
    kyoso_mei_ryakusho3 TEXT,               -- 競走名略称3文字 (6B)
    kyoso_mei_kubun     TEXT,               -- 競走名区分 (1B)
    jusho_kaiji         TEXT,               -- 重賞回次[第N回] (3B)
    grade_code          TEXT,               -- グレードコード (1B)
    henko_mae_grade_code TEXT,              -- 変更前グレードコード (1B)
    kyoso_shubetsu_code TEXT,               -- 競走種別コード (2B)
    kyoso_kigo_code     TEXT,               -- 競走記号コード (3B)
    juryo_shubetsu_code TEXT,               -- 重量種別コード (1B)
    kyoso_joken_code_2sai TEXT,             -- 競走条件コード 2歳条件 (3B)
    kyoso_joken_code_3sai TEXT,             -- 競走条件コード 3歳条件 (3B)
    kyoso_joken_code_4sai TEXT,             -- 競走条件コード 4歳条件 (3B)
    kyoso_joken_code_5sai_ijo TEXT,         -- 競走条件コード 5歳以上条件 (3B)
    kyoso_joken_code_saijakuinen TEXT,      -- 競走条件コード 最若年条件 (3B)
    kyoso_joken_meisho  TEXT,               -- 競走条件名称 (60B)
    kyori                TEXT,               -- 距離 (4B)
    henko_mae_kyori     TEXT,               -- 変更前距離 (4B)
    track_code          TEXT,               -- トラックコード (2B)
    henko_mae_track_code TEXT,              -- 変更前トラックコード (2B)
    course_kubun        TEXT,               -- コース区分 (2B)
    henko_mae_course_kubun TEXT,            -- 変更前コース区分 (2B)
    hon_shokin          TEXT,               -- 本賞金 (7×8B) JSON配列
    henko_mae_hon_shokin TEXT,              -- 変更前本賞金 (5×8B) JSON配列
    fuka_shokin         TEXT,               -- 付加賞金 (5×8B) JSON配列
    henko_mae_fuka_shokin TEXT,             -- 変更前付加賞金 (3×8B) JSON配列
    hasso_jikoku        TEXT,               -- 発走時刻 (4B)
    henko_mae_hasso_jikoku TEXT,            -- 変更前発走時刻 (4B)
    toroku_tosu         TEXT,               -- 登録頭数 (2B)
    shusso_tosu         TEXT,               -- 出走頭数 (2B)
    nyusen_tosu         TEXT,               -- 入線頭数 (2B)
    tenko_code          TEXT,               -- 天候コード (1B)
    shiba_baba_jotai_code TEXT,             -- 芝馬場状態コード (1B)
    dirt_baba_jotai_code TEXT,              -- ダート馬場状態コード (1B)
    lap_time            TEXT,               -- ラップタイム (25×3B) JSON配列
    shogai_mile_time    TEXT,               -- 障害マイルタイム (4B)
    mae_3f              TEXT,               -- 前3ハロン (3B)
    mae_4f              TEXT,               -- 前4ハロン (3B)
    ato_3f              TEXT,               -- 後3ハロン (3B)
    ato_4f              TEXT,               -- 後4ハロン (3B)
    corner_tsuka_juni   TEXT,               -- コーナー通過順位 (4回分) JSON
    record_koshin_kubun TEXT,               -- レコード更新区分 (1B)

    PRIMARY KEY (kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, race_bango)
);

-- ============================================================
-- 3. 馬毎レース情報 (SE) — 仕様書セクション3, Row 141-215
-- ============================================================
CREATE TABLE IF NOT EXISTS jvd_race_uma (
    -- ヘッダ
    record_shubetsu_id  TEXT NOT NULL,
    data_kubun          TEXT NOT NULL,
    data_sakusei_ymd    TEXT NOT NULL,

    -- キー項目 (○)
    kaisai_nen          TEXT NOT NULL,
    kaisai_tsukihi      TEXT NOT NULL,
    keibajo_code        TEXT NOT NULL,
    kaisai_kai          TEXT NOT NULL,
    kaisai_nichime      TEXT NOT NULL,
    race_bango          TEXT NOT NULL,
    umaban              TEXT NOT NULL,       -- 馬番 (2B)
    ketto_toroku_bango  TEXT NOT NULL,       -- 血統登録番号 (10B)

    -- 馬情報
    wakuban             TEXT,               -- 枠番 (1B)
    bamei               TEXT,               -- 馬名 (36B)
    uma_kigo_code       TEXT,               -- 馬記号コード (2B)
    seibetsu_code       TEXT,               -- 性別コード (1B)
    hinshu_code         TEXT,               -- 品種コード (1B)
    keiro_code          TEXT,               -- 毛色コード (2B)
    barei               TEXT,               -- 馬齢 (2B)
    tozai_shozoku_code  TEXT,               -- 東西所属コード (1B)
    chokyoshi_code      TEXT,               -- 調教師コード (5B)
    chokyoshi_mei_ryakusho TEXT,            -- 調教師名略称 (8B)
    banushi_code        TEXT,               -- 馬主コード (6B)
    banushi_mei         TEXT,               -- 馬主名(法人格無) (64B)
    fukushoku_hyoji     TEXT,               -- 服色標示 (60B)

    -- 斤量・騎手
    futan_juryo         TEXT,               -- 負担重量 (3B)
    henko_mae_futan_juryo TEXT,             -- 変更前負担重量 (3B)
    blinker_shiyou_kubun TEXT,              -- ブリンカー使用区分 (1B)
    kishu_code          TEXT,               -- 騎手コード (5B)
    henko_mae_kishu_code TEXT,              -- 変更前騎手コード (5B)
    kishu_mei_ryakusho  TEXT,               -- 騎手名略称 (8B)
    henko_mae_kishu_mei_ryakusho TEXT,      -- 変更前騎手名略称 (8B)
    kishu_minarai_code  TEXT,               -- 騎手見習コード (1B)
    henko_mae_kishu_minarai_code TEXT,      -- 変更前騎手見習コード (1B)

    -- 馬体重
    bataiju              TEXT,               -- 馬体重 (3B)
    zogen_fugo          TEXT,               -- 増減符号 (1B)
    zogen_sa            TEXT,               -- 増減差 (3B)

    -- 着順・結果
    ijo_kubun_code      TEXT,               -- 異常区分コード (1B)
    nyusen_juni         TEXT,               -- 入線順位 (2B)
    kakutei_chakujun    TEXT,               -- 確定着順 (2B)
    dochaku_kubun       TEXT,               -- 同着区分 (1B)
    dochaku_tosu        TEXT,               -- 同着頭数 (1B)
    soha_time           TEXT,               -- 走破タイム (4B)
    chakusa_code        TEXT,               -- 着差コード (3B)
    chakusa_code_plus   TEXT,               -- ＋着差コード (3B)
    chakusa_code_plus2  TEXT,               -- ＋＋着差コード (3B)

    -- コーナー通過順位
    corner1_juni        TEXT,               -- 1コーナーでの順位 (2B)
    corner2_juni        TEXT,               -- 2コーナーでの順位 (2B)
    corner3_juni        TEXT,               -- 3コーナーでの順位 (2B)
    corner4_juni        TEXT,               -- 4コーナーでの順位 (2B)

    -- オッズ・人気
    tansho_odds         TEXT,               -- 単勝オッズ (4B)
    tansho_ninki_jun    TEXT,               -- 単勝人気順 (2B)

    -- 賞金
    kakutoku_hon_shokin TEXT,               -- 獲得本賞金 (8B)
    kakutoku_fuka_shokin TEXT,              -- 獲得付加賞金 (8B)

    -- タイム
    ato_4f_time         TEXT,               -- 後4ハロンタイム (3B)
    ato_3f_time         TEXT,               -- 後3ハロンタイム (3B)

    -- 1着馬(相手馬)情報 (3頭分)
    aite_uma            TEXT,               -- JSON: [{ketto_toroku_bango, bamei}, ...]

    -- その他
    time_sa             TEXT,               -- タイム差 (4B)
    record_koshin_kubun TEXT,               -- レコード更新区分 (1B)
    mining_kubun        TEXT,               -- マイニング区分 (1B)
    mining_yoso_soha_time TEXT,             -- マイニング予想走破タイム (5B)
    mining_yoso_gosa_plus TEXT,             -- マイニング予想誤差＋ (4B)
    mining_yoso_gosa_minus TEXT,            -- マイニング予想誤差－ (4B)
    mining_yoso_juni    TEXT,               -- マイニング予想順位 (2B)
    kyakushitsu_hantei  TEXT,               -- 今回レース脚質判定 (1B)

    PRIMARY KEY (kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, race_bango, umaban, ketto_toroku_bango)
);

-- ============================================================
-- 13. 競走馬マスタ (UM) — 仕様書セクション13, Row 544-617
-- ============================================================
CREATE TABLE IF NOT EXISTS jvd_uma (
    -- ヘッダ
    record_shubetsu_id  TEXT NOT NULL,
    data_kubun          TEXT NOT NULL,
    data_sakusei_ymd    TEXT NOT NULL,

    -- キー項目 (○)
    ketto_toroku_bango  TEXT NOT NULL PRIMARY KEY,  -- 血統登録番号 (10B)

    -- 基本情報
    kyosouma_massho_kubun TEXT,             -- 競走馬抹消区分 (1B)
    kyosouma_toroku_ymd TEXT,               -- 競走馬登録年月日 (8B)
    kyosouma_massho_ymd TEXT,               -- 競走馬抹消年月日 (8B)
    seinengappi         TEXT,               -- 生年月日 (8B)
    bamei               TEXT,               -- 馬名 (36B)
    bamei_kana          TEXT,               -- 馬名半角ｶﾅ (36B)
    bamei_eiji          TEXT,               -- 馬名欧字 (60B)
    jra_shisetsu_zaikyu_flag TEXT,          -- JRA施設在きゅうフラグ (1B)
    uma_kigo_code       TEXT,               -- 馬記号コード (2B)
    seibetsu_code       TEXT,               -- 性別コード (1B)
    hinshu_code         TEXT,               -- 品種コード (1B)
    keiro_code          TEXT,               -- 毛色コード (2B)

    -- 3代血統情報 (14頭分: 父,母,父父,父母,母父,母母,父父父,父父母,父母父,父母母,母父父,母父母,母母父,母母母)
    -- JSON配列: [{hanshoku_toroku_bango, bamei}, ...]
    sandai_ketto        TEXT,               -- 3代血統情報 (14頭分)

    -- 所属
    tozai_shozoku_code  TEXT,               -- 東西所属コード (1B)
    chokyoshi_code      TEXT,               -- 調教師コード (5B)
    chokyoshi_mei_ryakusho TEXT,            -- 調教師名略称 (8B)
    shotai_chiiki_mei   TEXT,               -- 招待地域名 (20B)

    -- 生産・馬主
    seisansha_code      TEXT,               -- 生産者コード (8B)
    seisansha_mei       TEXT,               -- 生産者名(法人格無) (72B)
    sanchi_mei          TEXT,               -- 産地名 (20B)
    banushi_code        TEXT,               -- 馬主コード (6B)
    banushi_mei         TEXT,               -- 馬主名(法人格無) (64B)

    -- 賞金累計
    heichi_hon_shokin_ruikei  TEXT,         -- 平地本賞金累計 (9B)
    shogai_hon_shokin_ruikei  TEXT,         -- 障害本賞金累計 (9B)
    heichi_fuka_shokin_ruikei TEXT,         -- 平地付加賞金累計 (9B)
    shogai_fuka_shokin_ruikei TEXT,         -- 障害付加賞金累計 (9B)
    heichi_shutoku_shokin_ruikei TEXT,      -- 平地収得賞金累計 (9B)
    shogai_shutoku_shokin_ruikei TEXT,      -- 障害収得賞金累計 (9B)

    -- 着回数 (各6回分: 1着,2着,3着,4着,5着,着外)
    sogo_chakukaisu     TEXT,               -- 総合着回数 JSON [1着数,2着数,...,着外数]
    chuo_gokei_chakukaisu TEXT,             -- 中央合計着回数 JSON

    -- 馬場別着回数 JSON
    baba_betsu_chakukaisu TEXT,             -- {shiba_choku, shiba_migi, shiba_hidari, dirt_choku, dirt_migi, dirt_hidari, shogai}

    -- 馬場状態別着回数 JSON
    baba_jotai_betsu_chakukaisu TEXT,       -- {shiba_ryo, shiba_yaya, shiba_omo, shiba_fu, dirt_ryo, ...}

    -- 距離別着回数 JSON
    kyori_betsu_chakukaisu TEXT,            -- {shiba_16ka, shiba_22ka, shiba_22cho, dirt_16ka, dirt_22ka, dirt_22cho}

    -- 脚質傾向・登録レース数
    kyakushitsu_keiko   TEXT,               -- 脚質傾向 (4×3B) JSON
    toroku_race_su      TEXT                -- 登録レース数 (3B)
);

-- ============================================================
-- 18. 繁殖馬マスタ (HN) — 仕様書セクション18, Row 821-844
-- ============================================================
CREATE TABLE IF NOT EXISTS jvd_hanshoku (
    -- ヘッダ
    record_shubetsu_id  TEXT NOT NULL,
    data_kubun          TEXT NOT NULL,
    data_sakusei_ymd    TEXT NOT NULL,

    -- キー項目 (○)
    hanshoku_toroku_bango TEXT NOT NULL PRIMARY KEY,  -- 繁殖登録番号 (10B)

    -- 基本情報
    ketto_toroku_bango  TEXT,               -- 血統登録番号 (10B)
    bamei               TEXT,               -- 馬名 (36B)
    bamei_kana          TEXT,               -- 馬名半角ｶﾅ (40B)
    bamei_eiji          TEXT,               -- 馬名欧字 (80B)
    seinen              TEXT,               -- 生年 (4B)
    seibetsu_code       TEXT,               -- 性別コード (1B)
    hinshu_code         TEXT,               -- 品種コード (1B)
    keiro_code          TEXT,               -- 毛色コード (2B)
    hanshokuba_mochikomi_kubun TEXT,        -- 繁殖馬持込区分 (1B)
    yunyu_nen           TEXT,               -- 輸入年 (4B)
    sanchi_mei          TEXT,               -- 産地名 (20B)

    -- 血統リンク
    chichiuma_hanshoku_toroku_bango TEXT,   -- 父馬繁殖登録番号 (10B)
    hahauma_hanshoku_toroku_bango TEXT      -- 母馬繁殖登録番号 (10B)
);

-- ============================================================
-- 19. 産駒マスタ (SK) — 仕様書セクション19, Row 845-862
-- ============================================================
CREATE TABLE IF NOT EXISTS jvd_sanku (
    -- ヘッダ
    record_shubetsu_id  TEXT NOT NULL,
    data_kubun          TEXT NOT NULL,
    data_sakusei_ymd    TEXT NOT NULL,

    -- キー項目 (○)
    ketto_toroku_bango  TEXT NOT NULL PRIMARY KEY,  -- 血統登録番号 (10B)

    -- 基本情報
    seinengappi         TEXT,               -- 生年月日 (8B)
    seibetsu_code       TEXT,               -- 性別コード (1B)
    hinshu_code         TEXT,               -- 品種コード (1B)
    keiro_code          TEXT,               -- 毛色コード (2B)
    sanku_mochikomi_kubun TEXT,             -- 産駒持込区分 (1B)
    yunyu_nen           TEXT,               -- 輸入年 (4B)
    seisansha_code      TEXT,               -- 生産者コード (8B)
    sanchi_mei          TEXT,               -- 産地名 (20B)

    -- 3代血統 繁殖登録番号 (14頭分)
    sandai_ketto_hanshoku TEXT              -- JSON配列: [番号1, 番号2, ..., 番号14]
);

-- ============================================================
-- 14. 騎手マスタ (KS) — 仕様書セクション14, Row 618-707
-- ============================================================
CREATE TABLE IF NOT EXISTS jvd_kishu (
    -- ヘッダ
    record_shubetsu_id  TEXT NOT NULL,
    data_kubun          TEXT NOT NULL,
    data_sakusei_ymd    TEXT NOT NULL,

    -- キー項目 (○)
    kishu_code          TEXT NOT NULL PRIMARY KEY,  -- 騎手コード (5B)

    -- 基本情報
    kishu_massho_kubun  TEXT,               -- 騎手抹消区分 (1B)
    kishu_menkyo_kofu_ymd TEXT,             -- 騎手免許交付年月日 (8B)
    kishu_menkyo_massho_ymd TEXT,           -- 騎手免許抹消年月日 (8B)
    seinengappi         TEXT,               -- 生年月日 (8B)
    kishu_mei           TEXT,               -- 騎手名 (34B)
    kishu_mei_kana      TEXT,               -- 騎手名半角ｶﾅ (30B)
    kishu_mei_ryakusho  TEXT,               -- 騎手名略称 (8B)
    kishu_mei_eiji      TEXT,               -- 騎手名欧字 (80B)
    seibetsu_kubun      TEXT,               -- 性別区分 (1B)
    kijo_shikaku_code   TEXT,               -- 騎乗資格コード (1B)
    kishu_minarai_code  TEXT,               -- 騎手見習コード (1B)
    kishu_tozai_shozoku_code TEXT,          -- 騎手東西所属コード (1B)
    shotai_chiiki_mei   TEXT,               -- 招待地域名 (20B)
    shozoku_chokyoshi_code TEXT,            -- 所属調教師コード (5B)
    shozoku_chokyoshi_mei_ryakusho TEXT,    -- 所属調教師名略称 (8B)

    -- 初騎乗・初勝利・最近重賞 (JSON)
    hatsu_kijo          TEXT,               -- 初騎乗情報 (2回分) JSON
    hatsu_shori         TEXT,               -- 初勝利情報 (2回分) JSON
    saikin_jusho_shori  TEXT,               -- 最近重賞勝利情報 (3回分) JSON

    -- 本年・前年・累計成績 (3年分) JSON
    seiseki             TEXT                -- 成績情報 JSON
);

-- ============================================================
-- 15. 調教師マスタ (CH) — 仕様書セクション15, Row 708-778
-- ============================================================
CREATE TABLE IF NOT EXISTS jvd_chokyoshi (
    -- ヘッダ
    record_shubetsu_id  TEXT NOT NULL,
    data_kubun          TEXT NOT NULL,
    data_sakusei_ymd    TEXT NOT NULL,

    -- キー項目 (○)
    chokyoshi_code      TEXT NOT NULL PRIMARY KEY,  -- 調教師コード (5B)

    -- 基本情報
    chokyoshi_massho_kubun TEXT,            -- 調教師抹消区分 (1B)
    chokyoshi_menkyo_kofu_ymd TEXT,         -- 調教師免許交付年月日 (8B)
    chokyoshi_menkyo_massho_ymd TEXT,       -- 調教師免許抹消年月日 (8B)
    seinengappi         TEXT,               -- 生年月日 (8B)
    chokyoshi_mei       TEXT,               -- 調教師名 (34B)
    chokyoshi_mei_kana  TEXT,               -- 調教師名半角ｶﾅ (30B)
    chokyoshi_mei_ryakusho TEXT,            -- 調教師名略称 (8B)
    chokyoshi_mei_eiji  TEXT,               -- 調教師名欧字 (80B)
    seibetsu_kubun      TEXT,               -- 性別区分 (1B)
    chokyoshi_tozai_shozoku_code TEXT,      -- 調教師東西所属コード (1B)
    shotai_chiiki_mei   TEXT,               -- 招待地域名 (20B)

    -- 最近重賞勝利情報 (3回分) JSON
    saikin_jusho_shori  TEXT,

    -- 本年・前年・累計成績 (3年分) JSON
    seiseki             TEXT
);

-- ============================================================
-- 4. 払戻 (HR) — 仕様書セクション4, Row 216-297
-- ============================================================
CREATE TABLE IF NOT EXISTS jvd_haraimodoshi (
    -- ヘッダ
    record_shubetsu_id  TEXT NOT NULL,
    data_kubun          TEXT NOT NULL,
    data_sakusei_ymd    TEXT NOT NULL,

    -- キー項目 (○)
    kaisai_nen          TEXT NOT NULL,
    kaisai_tsukihi      TEXT NOT NULL,
    keibajo_code        TEXT NOT NULL,
    kaisai_kai          TEXT NOT NULL,
    kaisai_nichime      TEXT NOT NULL,
    race_bango          TEXT NOT NULL,

    -- 払戻情報 (各券種の組番・払戻金をJSON格納)
    tansho              TEXT,               -- 単勝 JSON [{umaban, haraimodoshi_kin}, ...]
    fukusho             TEXT,               -- 複勝 JSON
    wakuren             TEXT,               -- 枠連 JSON
    umaren              TEXT,               -- 馬連 JSON
    wide                TEXT,               -- ワイド JSON
    umatan              TEXT,               -- 馬単 JSON
    sanrenpuku          TEXT,               -- 3連複 JSON
    sanrentan           TEXT,               -- 3連単 JSON

    PRIMARY KEY (kaisai_nen, kaisai_tsukihi, keibajo_code, kaisai_kai, kaisai_nichime, race_bango)
);

-- ============================================================
-- 16. 生産者マスタ (BR) — 仕様書セクション16, Row 779-799
-- ============================================================
CREATE TABLE IF NOT EXISTS jvd_seisansha (
    record_shubetsu_id  TEXT NOT NULL,
    data_kubun          TEXT NOT NULL,
    data_sakusei_ymd    TEXT NOT NULL,

    seisansha_code      TEXT NOT NULL PRIMARY KEY,  -- 生産者コード (8B)

    seisansha_mei_hojinkaku_ari TEXT,       -- 生産者名(法人格有) (72B)
    seisansha_mei       TEXT,               -- 生産者名(法人格無) (72B)
    seisansha_mei_kana  TEXT,               -- 生産者名半角ｶﾅ (72B)
    seisansha_mei_eiji  TEXT,               -- 生産者名欧字 (168B)
    seisansha_jusho     TEXT,               -- 生産者住所自治省名 (20B)

    -- 本年・累計成績 (2年分) JSON
    seiseki             TEXT
);

-- ============================================================
-- 17. 馬主マスタ (BN) — 仕様書セクション17, Row 800-820
-- ============================================================
CREATE TABLE IF NOT EXISTS jvd_banushi (
    record_shubetsu_id  TEXT NOT NULL,
    data_kubun          TEXT NOT NULL,
    data_sakusei_ymd    TEXT NOT NULL,

    banushi_code        TEXT NOT NULL PRIMARY KEY,  -- 馬主コード (6B)

    banushi_mei_hojinkaku_ari TEXT,         -- 馬主名(法人格有) (64B)
    banushi_mei         TEXT,               -- 馬主名(法人格無) (64B)
    banushi_mei_kana    TEXT,               -- 馬主名半角ｶﾅ (50B)
    banushi_mei_eiji    TEXT,               -- 馬主名欧字 (100B)
    fukushoku_hyoji     TEXT,               -- 服色標示 (60B)

    -- 本年・累計成績 (2年分) JSON
    seiseki             TEXT
);

-- ============================================================
-- インデックス
-- ============================================================

-- レース検索用
CREATE INDEX IF NOT EXISTS idx_race_date ON jvd_race (kaisai_nen, kaisai_tsukihi);
CREATE INDEX IF NOT EXISTS idx_race_keibajo ON jvd_race (keibajo_code);

-- 馬毎レース情報: 馬から成績検索
CREATE INDEX IF NOT EXISTS idx_race_uma_ketto ON jvd_race_uma (ketto_toroku_bango);
CREATE INDEX IF NOT EXISTS idx_race_uma_kishu ON jvd_race_uma (kishu_code);
CREATE INDEX IF NOT EXISTS idx_race_uma_chokyoshi ON jvd_race_uma (chokyoshi_code);

-- 繁殖馬: 血統ツリー辿り用
CREATE INDEX IF NOT EXISTS idx_hanshoku_chichi ON jvd_hanshoku (chichiuma_hanshoku_toroku_bango);
CREATE INDEX IF NOT EXISTS idx_hanshoku_haha ON jvd_hanshoku (hahauma_hanshoku_toroku_bango);
CREATE INDEX IF NOT EXISTS idx_hanshoku_ketto ON jvd_hanshoku (ketto_toroku_bango);

-- 競走馬マスタ: 馬名検索
CREATE INDEX IF NOT EXISTS idx_uma_bamei ON jvd_uma (bamei);

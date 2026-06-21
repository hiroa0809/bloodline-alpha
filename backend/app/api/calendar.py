"""
開催カレンダーAPI（新馬戦専用・Phase 1）

GET /api/v1/calendar/dates  — 新馬戦のある開催日を新しい順に一覧（開催場・レース数つき）
GET /api/v1/calendar/races  — 指定開催日の新馬戦レース一覧（出馬表へ遷移するための情報）

16桁レースID手入力を不要にするための画面用API（#10c/#10d）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.score import derive_race_name

router = APIRouter(prefix="/api/v1/calendar", tags=["Calendar"])

# 競馬場コード（JV-Data 2001）→ 名称。JRA10場のみ（Phase1対象）。
KEIBAJO_NAME = {
    "01": "札幌",
    "02": "函館",
    "03": "福島",
    "04": "新潟",
    "05": "東京",
    "06": "中山",
    "07": "中京",
    "08": "京都",
    "09": "阪神",
    "10": "小倉",
}

# 新馬戦の抽出条件（2歳条件 or 3歳条件が 701=新馬）
_SHINBA_WHERE = "(kyoso_joken_code_2sai = '701' OR kyoso_joken_code_3sai = '701')"


def track_surface(track_code: str | None) -> str:
    """track_code 先頭1桁 → 芝/ダート/障（表示用）。"""
    first = (track_code or "").strip()[:1]
    if first == "1":
        return "芝"
    if first == "2":
        return "ダート"
    if first in ("3", "5", "6", "7"):
        return "障害"
    return ""


def fmt_hasso(hhmm: str | None) -> str:
    """発走時刻 'HHMM' → 'HH:MM'。"""
    s = (hhmm or "").strip()
    return f"{s[:2]}:{s[2:]}" if len(s) == 4 else ""


# --- レスポンスモデル ---


class KaisaiDate(BaseModel):
    date: str  # YYYYMMDD
    display: str  # YYYY/MM/DD
    venues: list[str]  # 開催場名
    race_count: int  # その日の新馬戦数


class RaceListItem(BaseModel):
    race_id: str  # 16桁
    keibajo: str  # 競馬場名
    race_bango: int  # R番号
    race_name: str  # 例: 3歳新馬
    surface: str  # 芝/ダート/障害
    kyori: int  # 距離(m)
    shusso_tosu: int  # 出走頭数
    hasso: str  # 発走時刻 HH:MM


# --- エンドポイント ---


@router.get("/dates", response_model=list[KaisaiDate])
async def list_dates(
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """新馬戦のある開催日を新しい順に返す（ページング対応 = もっと見る）。"""
    # ① 対象となる開催日（新しい順）を limit/offset で確定
    date_rows = (
        await db.execute(
            text(
                "SELECT kaisai_nen, kaisai_tsukihi FROM jvd_race "
                f"WHERE {_SHINBA_WHERE} "
                "GROUP BY kaisai_nen, kaisai_tsukihi "
                "ORDER BY kaisai_nen DESC, kaisai_tsukihi DESC "
                "LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
    ).fetchall()
    if not date_rows:
        return []

    dates = [(r[0], r[1]) for r in date_rows]
    nen_set = {d[0] for d in dates}
    tsukihi_set = {d[1] for d in dates}

    # ② 上記開催日の「開催場ごとの新馬戦数」を取得（年・月日で粗く絞り Python 側で厳密照合）
    detail_rows = (
        await db.execute(
            text(
                "SELECT kaisai_nen, kaisai_tsukihi, keibajo_code, COUNT(*) AS cnt "
                "FROM jvd_race "
                f"WHERE {_SHINBA_WHERE} "
                "  AND kaisai_nen IN :nens AND kaisai_tsukihi IN :tsukihis "
                "GROUP BY kaisai_nen, kaisai_tsukihi, keibajo_code"
            ).bindparams(
                # IN 句の展開
                bindparam("nens", expanding=True),
                bindparam("tsukihis", expanding=True),
            ),
            {"nens": list(nen_set), "tsukihis": list(tsukihi_set)},
        )
    ).fetchall()

    # (年, 月日) → [(場名, 件数)] に集約
    agg: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for nen, tsukihi, jo, cnt in detail_rows:
        agg.setdefault((nen, tsukihi), []).append((jo, cnt))

    result: list[KaisaiDate] = []
    for nen, tsukihi in dates:
        venues = sorted(agg.get((nen, tsukihi), []), key=lambda x: x[0])
        result.append(
            KaisaiDate(
                date=f"{nen}{tsukihi}",
                display=f"{nen}/{tsukihi[:2]}/{tsukihi[2:]}",
                venues=[KEIBAJO_NAME.get(jo, jo) for jo, _ in venues],
                race_count=sum(c for _, c in venues),
            )
        )
    return result


@router.get("/races", response_model=list[RaceListItem])
async def list_races(
    date: str = Query(..., min_length=8, max_length=8, pattern=r"^\d{8}$"),
    db: AsyncSession = Depends(get_db),
):
    """指定開催日（YYYYMMDD）の新馬戦レースを場・R番号順に返す。"""
    nen, tsukihi = date[:4], date[4:]
    rows = (
        await db.execute(
            text(
                "SELECT keibajo_code, kaisai_kai, kaisai_nichime, race_bango, "
                "  kyoso_mei_hondai, kyoso_mei_ryakusho10, kyoso_shubetsu_code, "
                "  kyoso_joken_code_2sai, kyoso_joken_code_3sai, "
                "  kyoso_joken_code_4sai, kyoso_joken_code_5sai_ijo, "
                "  kyori, track_code, shusso_tosu, hasso_jikoku "
                "FROM jvd_race "
                f"WHERE kaisai_nen = :nen AND kaisai_tsukihi = :tsukihi AND {_SHINBA_WHERE} "
                "ORDER BY keibajo_code, race_bango"
            ),
            {"nen": nen, "tsukihi": tsukihi},
        )
    ).fetchall()

    items: list[RaceListItem] = []
    for r in rows:
        jo, kai, nichime, rno = r[0], r[1], r[2], r[3]
        # 16桁 race_id 契約を保証するため各2桁要素をゼロ埋め（年4+月日4+場2+回2+日2+R2）。
        # 競馬場名ルックアップ等でも同じ正規化値を使い不整合を防ぐ。
        jo_s = str(jo).zfill(2)
        kai_s = str(kai).zfill(2)
        nichime_s = str(nichime).zfill(2)
        rno_s = str(rno).zfill(2)
        race_id = f"{nen}{tsukihi}{jo_s}{kai_s}{nichime_s}{rno_s}"
        race_name = derive_race_name(
            r[4],
            r[5],
            r[6],
            {"2sai": r[7], "3sai": r[8], "4sai": r[9], "5sai_ijo": r[10]},
        )
        items.append(
            RaceListItem(
                race_id=race_id,
                keibajo=KEIBAJO_NAME.get(jo_s, jo_s),
                race_bango=int(rno_s) if rno_s.isdigit() else 0,
                race_name=race_name,
                surface=track_surface(r[12]),
                kyori=int(r[11]) if str(r[11]).isdigit() else 0,
                shusso_tosu=int(r[13]) if str(r[13]).isdigit() else 0,
                hasso=fmt_hasso(r[14]),
            )
        )
    return items

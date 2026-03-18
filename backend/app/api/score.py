"""
スコアリングAPI

GET /api/v1/score/{race_id} — 実データによる血統スコアリング
GET /api/v1/score/mock/{race_id} — デモ用モックデータ
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services.bloodline_score import calc_bloodline_score

router = APIRouter(prefix="/api/v1/score", tags=["Scoring"])


# --- Pydantic レスポンスモデル（Swagger UI 表示用） ---

class SireInfo(BaseModel):
    name: str
    hanshoku_bango: str
    win_rate: float
    roi: float


class CategoryDetail(BaseModel):
    total: float
    details: dict[str, float]


class PredictionItem(BaseModel):
    horse_number: int
    horse_name: str
    ketto_toroku_bango: str
    odds: float
    popularity: int
    total_score: float
    category_scores: dict[str, CategoryDetail]
    sire_info: SireInfo | None = None
    bms_info: SireInfo | None = None


class RaceScoreResponse(BaseModel):
    race_id: str
    race_name: str
    predictions: list[PredictionItem]


# --- ユーティリティ ---

def parse_race_id(race_id: str) -> dict:
    """16桁の race_id を6カラムPKに分解"""
    if len(race_id) != 16:
        raise HTTPException(
            status_code=400,
            detail=f"race_id は16桁である必要があります（入力: {len(race_id)}桁）",
        )
    return {
        "kaisai_nen": race_id[0:4],
        "kaisai_tsukihi": race_id[4:8],
        "keibajo_code": race_id[8:10],
        "kaisai_kai": race_id[10:12],
        "kaisai_nichime": race_id[12:14],
        "race_bango": race_id[14:16],
    }


def parse_odds(odds_str: str | None) -> float:
    """JV-Data 単勝オッズ（4桁文字列）を float に変換"""
    try:
        return int(odds_str) / 10.0
    except (ValueError, TypeError):
        return 0.0


# --- エンドポイント ---

@router.get("/mock/{race_id}")
async def get_mock_score(race_id: str):
    """[デモ用] 固定のダミースコアを返す"""
    return {
        "race_id": race_id,
        "predictions": [
            {"horse_number": 1, "horse_name": "リアルスティール産駒", "score": 85, "score_details": {"bloodline": 55, "condition": 18, "human": 12}, "odds": 5.5, "popularity": 3, "expected_value": 1.45},
            {"horse_number": 2, "horse_name": "過剰人気馬", "score": 45, "score_details": {"bloodline": 20, "condition": 15, "human": 10}, "odds": 1.8, "popularity": 1, "expected_value": 0.55},
            {"horse_number": 3, "horse_name": "穴馬", "score": 75, "score_details": {"bloodline": 50, "condition": 15, "human": 10}, "odds": 25.0, "popularity": 8, "expected_value": 1.30},
        ],
    }


@router.get("/{race_id}", response_model=RaceScoreResponse)
async def get_score(race_id: str, db: AsyncSession = Depends(get_db)):
    """
    指定レースの全出走馬に対して血統スコア（カテゴリA）を計算して返す。
    race_id: 16桁（年4+月日4+競馬場2+回2+日目2+レース番号2）
    """
    pk = parse_race_id(race_id)

    # レース情報を取得
    race_result = await db.execute(
        text(
            "SELECT kyoso_mei_hondai, kyoso_mei_ryakusho10, kyori, track_code "
            "FROM jvd_race "
            "WHERE kaisai_nen = :kaisai_nen AND kaisai_tsukihi = :kaisai_tsukihi "
            "  AND keibajo_code = :keibajo_code AND kaisai_kai = :kaisai_kai "
            "  AND kaisai_nichime = :kaisai_nichime AND race_bango = :race_bango"
        ),
        pk,
    )
    race = race_result.fetchone()
    if not race:
        raise HTTPException(status_code=404, detail="レースが見つかりません")

    race_name = (race[0] or race[1] or "").strip()

    # 出走馬一覧を取得
    uma_result = await db.execute(
        text(
            "SELECT umaban, bamei, ketto_toroku_bango, tansho_odds, tansho_ninki_jun "
            "FROM jvd_race_uma "
            "WHERE kaisai_nen = :kaisai_nen AND kaisai_tsukihi = :kaisai_tsukihi "
            "  AND keibajo_code = :keibajo_code AND kaisai_kai = :kaisai_kai "
            "  AND kaisai_nichime = :kaisai_nichime AND race_bango = :race_bango "
            "ORDER BY CAST(umaban AS INTEGER)"
        ),
        pk,
    )
    umas = uma_result.fetchall()
    if not umas:
        raise HTTPException(status_code=404, detail="出走馬が見つかりません")

    # 各出走馬の血統スコアを計算
    predictions = []
    for uma in umas:
        umaban, bamei, ketto_bango, odds_str, ninki_str = uma

        bloodline = await calc_bloodline_score(db, ketto_bango)

        predictions.append(
            PredictionItem(
                horse_number=int(umaban or 0),
                horse_name=(bamei or "").strip(),
                ketto_toroku_bango=ketto_bango or "",
                odds=parse_odds(odds_str),
                popularity=int(ninki_str or 0),
                total_score=bloodline["total"],
                category_scores={
                    "A": CategoryDetail(
                        total=bloodline["total"],
                        details={
                            "A1": bloodline["A1"],
                            "A2": bloodline["A2"],
                            "A3": bloodline["A3"],
                            "A4": bloodline["A4"],
                            "A5": bloodline["A5"],
                        },
                    ),
                    "B": CategoryDetail(total=0, details={}),
                    "C": CategoryDetail(total=0, details={}),
                    "D": CategoryDetail(total=0, details={}),
                    "E": CategoryDetail(total=0, details={}),
                },
                sire_info=SireInfo(**bloodline["sire_info"]) if bloodline["sire_info"] else None,
                bms_info=SireInfo(**bloodline["bms_info"]) if bloodline["bms_info"] else None,
            )
        )

    # スコア降順ソート
    predictions.sort(key=lambda x: x.total_score, reverse=True)

    return RaceScoreResponse(
        race_id=race_id,
        race_name=race_name,
        predictions=predictions,
    )

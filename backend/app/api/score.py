from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload
from decimal import Decimal

from app.core.database import get_db
from app.models.models import Horse, Race, RaceResult, Pedigree, SireStat

router = APIRouter(prefix="/api/v1/score", tags=["Scoring"])

@router.get("/mock/{race_id}")
async def get_mock_score(race_id: str, db: AsyncSession = Depends(get_db)):
    """
    [MVP用モック] 
    要求されたレースに対して、全出走馬のダミースコアと市場乖離を返す。
    本来はここで100点満点のスコアと期待値を計算するが、取得データが貯まるまでの繋ぎ。
    """
    
    # 1. データベースから出走馬一覧を取得
    stmt = select(RaceResult).filter(RaceResult.race_id == race_id).order_by(RaceResult.horse_number)
    result = await db.execute(stmt)
    race_results = result.scalars().all()
    
    if not race_results:
        # デモ用に固定のダミーデータを返す（UI開発用）
        return {
            "race_id": race_id,
            "predictions": [
                {"horse_id": "dummy1", "horse_number": 1, "horse_name": "リアルスティール産駒", "score": 85, "score_details": {"bloodline": 55, "condition": 18, "human": 12}, "odds": 5.5, "popularity": 3, "expected_value": 1.45},
                {"horse_id": "dummy2", "horse_number": 2, "horse_name": "過剰人気馬", "score": 45, "score_details": {"bloodline": 20, "condition": 15, "human": 10}, "odds": 1.8, "popularity": 1, "expected_value": 0.55},
                {"horse_id": "dummy3", "horse_number": 3, "horse_name": "穴馬", "score": 75, "score_details": {"bloodline": 50, "condition": 15, "human": 10}, "odds": 25.0, "popularity": 8, "expected_value": 1.30}
            ]
        }

    # 2. ここから先は実データがある場合の処理
    predictions = []
    for r in race_results:
        # 馬情報取得
        horse_stmt = select(Horse).filter(Horse.horse_id == r.horse_id)
        h_res = await db.execute(horse_stmt)
        horse = h_res.scalars().first()
        horse_name = horse.name if horse else f"Horse_{r.horse_number}"
        
        # 本格実装(Phase1)ではここで sire_stats 等から点数計算を実施する
        # 現状はオッズの逆相関にランダム性を加えたモックスコアを生成
        base_score = 100 - (float(r.odds) if r.odds else 50)
        mock_score = max(10, min(95, base_score + (hash(r.horse_id) % 20 - 10)))
        
        # 内訳の疑似計算 (血統65点、適性20点、陣営15点満点に近い比率で分割)
        b_score = mock_score * 0.65
        c_score = mock_score * 0.20
        h_score = mock_score * 0.15
        
        # 期待値の疑似計算
        prob = mock_score / 100.0 * 0.5 # 勝率の概念
        ev = prob * float(r.odds) if r.odds else 0.8

        predictions.append({
            "horse_id": r.horse_id,
            "horse_number": r.gate_number or r.horse_number,
            "horse_name": horse_name,
            "jockey": r.jockey,
            "odds": float(r.odds) if r.odds else 0.0,
            "popularity": r.popularity,
            "score": round(mock_score, 1),
            "score_details": {
                "bloodline": round(b_score, 1),
                "condition": round(c_score, 1),
                "human": round(h_score, 1)
            },
            "expected_value": round(ev, 2)
        })
        
    # スコア降順でソート
    predictions.sort(key=lambda x: x["score"], reverse=True)
    
    return {
        "race_id": race_id,
        "predictions": predictions
    }

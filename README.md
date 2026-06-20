# bloodline-alpha

JRA競馬の血統データ分析ダッシュボード。ローカルに取得した競馬データ（JV-Data 形式）を解析・SQLite に格納し、血統や各種条件をもとにしたスコアリング結果を Web UI で表示する。

## アーキテクチャ

データインポーター → Backend API → Frontend の3層構成。SQLite を共有DBとして使用する。

| レイヤ | 技術 | 役割 |
|---|---|---|
| `scraper/` | Python | ローカルの JV-Data 固定長バイナリ（Shift-JIS）を解析し SQLite へ格納 |
| `backend/` | FastAPI + SQLAlchemy + aiosqlite | スコアリング結果を JSON で返す非同期 API |
| `frontend/` | Next.js + React + Tailwind CSS | スコアを一覧表示するダッシュボード |

## セットアップ

### 必要環境
- Python 3.11+
- Node.js 20+

### Backend

```bash
pip install sqlalchemy aiosqlite fastapi uvicorn openpyxl
cd backend
python -m app.main          # → http://localhost:8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev                 # → http://localhost:3000
```

## データについて

競馬データ本体（`backend/bloodline.db` および JV-Data 元ファイル）はリポジトリに含まない。各自がローカルで用意し、`scraper/` のインポーターで生成する。

## ステータス

Phase 1（MVP）。データ解析・SQLite 格納とスコアリングの一部を実装済み。

## ライセンス

未定（個人開発）。

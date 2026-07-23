import os
import asyncio
from typing import List, Optional
from fastapi import FastAPI, Query, HTTPException, Depends, status, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import asyncpg

from epo_service import EPOOPSService
from websocket_manager import AuctionWebSocketManager
from ai_matcher import AIPatentMatcher
from scraper import ConstructionTenderScraper
from data_room import SecureDataRoomService

# ====================================================================
# FASTAPI GÖRÜŞÜLEN UYGULAMA VE SOKET YÖNETİCİSİ
# ====================================================================
app = FastAPI(
    title="Patent Pazarı Enterprise Backend API",
    description="Canlı İhale WebSockets, AI Vektör Eşleşme, EKAP Scraper Botu & AWS S3 Secure Data Room API",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_DSN = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/patent_pazari")
db_pool: Optional[asyncpg.Pool] = None
ws_manager = AuctionWebSocketManager()


@app.on_event("startup")
async def startup_db_pool():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=2, max_size=20, command_timeout=60.0)
        print("PostgreSQL Veritabanı Bağlantı Havuzu Başarıyla Başlatıldı.")
    except Exception as e:
        print(f"Veritabanı Bağlantı Hatası: {e}")


@app.on_event("shutdown")
async def shutdown_db_pool():
    global db_pool
    if db_pool:
        await db_pool.close()


async def get_db():
    if db_pool is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="DB servisi hazır değil.")
    return db_pool


# DTO Models
class SubmitBidDTO(BaseModel):
    request_id: str
    bidder_id: str
    patent_id: Optional[str] = None
    bid_amount_try: float
    proposal_note: str


class NDARequestDTO(BaseModel):
    user_id: str
    patent_id: str


# ====================================================================
# MODÜL 1: REAL-TIME BIDDING WEBSOCKET ENDPOINT
# ====================================================================
@app.websocket("/ws/auction/{request_id}")
async def websocket_auction_endpoint(websocket: WebSocket, request_id: str):
    """
    Canlı İhale Canlı Teklif Odası.
    WebSocket ile baglanan kullanıcılara anlık ihale tekliflerini push eder.
    """
    await ws_manager.connect(request_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # İhale teklifi veya ping dinle
            if data.get("action") == "submit_bid":
                payload = await ws_manager.submit_bid_atomic(
                    db_pool=db_pool,
                    request_id=request_id,
                    bidder_id=data["bidder_id"],
                    patent_id=data.get("patent_id"),
                    bid_amount_try=float(data["bid_amount_try"]),
                    proposal_note=data.get("proposal_note", "")
                )
                await websocket.send_json({"status": "BID_ACCEPTED", "payload": payload})
    except WebSocketDisconnect:
        await ws_manager.disconnect(request_id, websocket)
    except Exception as e:
        await websocket.send_json({"status": "ERROR", "message": str(e)})
        await ws_manager.disconnect(request_id, websocket)


@app.post("/api/v2/auction/submit-bid", summary="Canlı İhale Teklifi Ver (Atomic SQL)")
async def submit_bid_http_endpoint(dto: SubmitBidDTO, pool: asyncpg.Pool = Depends(get_db)):
    try:
        result = await ws_manager.submit_bid_atomic(
            db_pool=pool,
            request_id=dto.request_id,
            bidder_id=dto.bidder_id,
            patent_id=dto.patent_id,
            bid_amount_try=dto.bid_amount_try,
            proposal_note=dto.proposal_note
        )
        return {"status": "success", "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ====================================================================
# MODÜL 2: AI VECTOR ANLAMSAL EŞLEŞTİRME ENDPOINT
# ====================================================================
@app.get("/api/v2/ai/match-patents", summary="Yapay Zeka Anlamsal Patent Eşleştirme")
async def ai_match_patents_endpoint(
    problem_statement: str = Query(..., description="Alıcının çözmek istediği inşaat problemi"),
    top_k: int = Query(5, ge=1, le=20),
    pool: asyncpg.Pool = Depends(get_db)
):
    matcher = AIPatentMatcher(db_pool=pool)
    matches = await matcher.find_matching_patents_for_request(problem_statement, top_k=top_k)
    return {
        "status": "success",
        "query_problem": problem_statement,
        "matched_patents_count": len(matches),
        "matches": matches
    }


# ====================================================================
# MODÜL 3: OTOMATİK EKAP & İNŞAAT İHALE SCRAPER BOTU
# ====================================================================
@app.post("/api/v2/scraper/ingest-construction-tenders", summary="Kamu EKAP / İhale Scraper Botunu Tetikle")
async def trigger_scraper_endpoint(pool: asyncpg.Pool = Depends(get_db)):
    scraper = ConstructionTenderScraper(db_pool=pool)
    count = await scraper.run_scraper_ingestion()
    return {
        "status": "success",
        "ingested_tenders_count": count,
        "message": f"{count} adet yeni inşaat ihale talebi veritabanına aktarıldı."
    }


# ====================================================================
# MODÜL 4: SECURE DATA ROOM & DİJİTAL NDA ENDPOINTS
# ====================================================================
@app.post("/api/v2/data-room/accept-nda", summary="Dijital Gizlilik Sözleşmesi (NDA) İmzala")
async def accept_nda_endpoint(dto: NDARequestDTO, request: Request, pool: asyncpg.Pool = Depends(get_db)):
    service = SecureDataRoomService(db_pool=pool)
    client_ip = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent", "Unknown")

    res = await service.accept_digital_nda(dto.user_id, dto.patent_id, client_ip, user_agent)
    return res


@app.get("/api/v2/data-room/presigned-url", summary="15 Dakikalık S3 Presigned Belge İndirme Adresi Al")
async def get_presigned_url_endpoint(
    user_id: str = Query(...),
    patent_id: str = Query(...),
    document_id: str = Query("doc_default_01"),
    pool: asyncpg.Pool = Depends(get_db)
):
    service = SecureDataRoomService(db_pool=pool)
    try:
        link_info = await service.generate_presigned_download_url(user_id, patent_id, document_id)
        return link_info
    except PermissionError as pe:
        raise HTTPException(status_code=403, detail=str(pe))


# ====================================================================
# MEVCUT ARAMA & IPC ENDPOINTLERİ
# ====================================================================
@app.get("/api/v1/search/construction", summary="İnşaat Sektörü Arama Motoru")
async def search_construction_market(
    ipc_code: Optional[str] = Query(None),
    search_term: Optional[str] = Query(None),
    min_budget: Optional[float] = Query(None),
    max_budget: Optional[float] = Query(None),
    deal_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    pool: asyncpg.Pool = Depends(get_db)
):
    offset = (page - 1) * page_size
    async with pool.acquire() as conn:
        patent_sql = """
            SELECT DISTINCT p.id::text, p.patent_number, p.title, p.abstract,
                   u.company_name, p.listing_type, p.min_expectation_try,
                   ARRAY_AGG(pic.ipc_code) OVER (PARTITION BY p.id) as ipc_codes
            FROM patents p
            JOIN users u ON p.owner_id = u.id
            JOIN patent_ipc_categories pic ON p.id = pic.patent_id
            JOIN ipc_categories cat ON pic.ipc_code = cat.code
            WHERE p.is_active = TRUE AND (cat.path <@ 'E'::ltree OR cat.is_construction_sector = TRUE)
        """
        patent_params = []
        param_idx = 1

        if ipc_code:
            patent_sql += f" AND pic.ipc_code LIKE ${param_idx} || '%'"
            patent_params.append(ipc_code.upper())
            param_idx += 1

        if search_term:
            patent_sql += f" AND (p.title ILIKE ${param_idx} OR p.abstract ILIKE ${param_idx})"
            patent_params.append(f"%{search_term}%")
            param_idx += 1

        patent_sql += f" ORDER BY p.created_at DESC LIMIT {page_size} OFFSET {offset}"
        rows = await conn.fetch(patent_sql, *patent_params)

        patents_list = [
            {
                "id": r["id"],
                "patent_number": r["patent_number"],
                "title": r["title"],
                "abstract": r["abstract"],
                "owner_company": r["company_name"],
                "listing_type": r["listing_type"],
                "min_expectation_try": float(r["min_expectation_try"]) if r["min_expectation_try"] else None,
                "ipc_codes": list(r["ipc_codes"]) if r["ipc_codes"] else []
            }
            for r in rows
        ]

        return {"total_count": len(patents_list), "patents": patents_list}

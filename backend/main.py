import os
import asyncio
from typing import List, Optional
from fastapi import FastAPI, Query, HTTPException, Depends, status, WebSocket, WebSocketDisconnect, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import asyncpg

from epo_service import EPOOPSService
from websocket_manager import AuctionWebSocketManager
from ai_matcher import AIPatentMatcher
from scraper import ConstructionTenderScraper
from data_room import SecureDataRoomService
from escrow_service import EscrowFintechService
from ai_vector_engine import GGUFVectorMatcherEngine

# ====================================================================
# FASTAPI ENTERPRISE UYGULAMA VE SOKET YÖNETİCİSİ
# ====================================================================
app = FastAPI(
    title="Patent Pazarı Enterprise Backend API",
    description="AWS EC2 Odysseus GGUF & pgvector Akıllı Eşleştirme, Escrow, WebSockets Canlı İhale & AWS S3 Data Room",
    version="2.2.0",
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


class ChargeEscrowDTO(BaseModel):
    buyer_id: str
    seller_id: str
    total_amount_try: float
    patent_id: Optional[str] = None
    request_id: Optional[str] = None
    card_token: Optional[str] = "tok_mock_card_1234"


class ReleaseEscrowDTO(BaseModel):
    admin_user_id: str
    notary_approval_document_no: str


# ====================================================================
# MODÜL 6: GGUF & PGVECTOR AKILLI KOSİNÜS EŞLEŞTİRME API'LERİ
# ====================================================================
@app.get("/api/v2/ai/smart-match", summary="GGUF & pgvector HNSW Kosinüs Benzerliği İle Akıllı Patent Eşleştirme", tags=["AI & pgvector Engine"])
async def smart_match_patents_endpoint(
    query_text: str = Query(..., description="Alıcının aradığı teknik problem metni"),
    threshold: float = Query(0.50, ge=0.0, le=1.0, description="Asgari Kosinüs Uyum Eşiği (Örn. 0.50 = %50)"),
    top_k: int = Query(5, ge=1, le=20, description="Getirilecek Maksimum Patent Sayısı"),
    pool: asyncpg.Pool = Depends(get_db)
):
    """
    Tencent Hy3 / Llama GGUF yerel modeli ile alıcının aradığı problemi 1536-boyutlu vektöre dönüştürür.
    PostgreSQL pgvector HNSW indeksi üzerinden kosinüs benzerliği hesabı yapar ve % uyum skoru yüksek olanları sıralar.
    """
    engine = GGUFVectorMatcherEngine(db_pool=pool)
    try:
        matches = await engine.find_matching_patents_for_query(
            query_text=query_text,
            similarity_threshold=threshold,
            top_k=top_k
        )
        return {
            "status": "success",
            "query_text": query_text,
            "threshold_used": threshold,
            "matched_count": len(matches),
            "matches": matches
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Vektör Eşleştirme hatası: {str(e)}")


@app.post("/api/v2/ai/vectorize-patent/{patent_id}", summary="Patent İçeriğini Vektörleştir ve pgvector'e Kaydet", tags=["AI & pgvector Engine"])
async def vectorize_patent_endpoint(
    patent_id: str,
    text_content: str = Query(..., description="Patent Başlığı ve Özeti"),
    bg_tasks: BackgroundTasks = None,
    pool: asyncpg.Pool = Depends(get_db)
):
    engine = GGUFVectorMatcherEngine(db_pool=pool)
    if bg_tasks:
        bg_tasks.add_task(engine.vectorize_and_save_patent, patent_id, text_content)
        return {"status": "processing", "message": f"Patent {patent_id} için vektörleştirme arka plana alındı."}
    else:
        await engine.vectorize_and_save_patent(patent_id, text_content)
        return {"status": "success", "message": f"Patent {patent_id} başarıyla vektörleştirildi."}


# ====================================================================
# ESCROW (GÜVENLİ HAVUZ) API'LERİ
# ====================================================================
@app.post("/api/v2/escrow/charge", summary="Alıcı Kredi Kartından Parayı Çek ve Havuzda Bloke Et", tags=["Escrow & Fintech"])
async def charge_escrow_endpoint(dto: ChargeEscrowDTO, pool: asyncpg.Pool = Depends(get_db)):
    service = EscrowFintechService(db_pool=pool)
    try:
        res = await service.charge_buyer_to_escrow(
            buyer_id=dto.buyer_id, seller_id=dto.seller_id, total_amount=dto.total_amount_try,
            patent_id=dto.patent_id, request_id=dto.request_id, card_token=dto.card_token
        )
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v2/escrow/release/{transaction_id}", summary="Admin Noter Devri Onayı ve Satıcıya Payout (%5 Komisyon Kesintisi)", tags=["Escrow & Fintech"])
async def release_escrow_endpoint(transaction_id: str, dto: ReleaseEscrowDTO, pool: asyncpg.Pool = Depends(get_db)):
    service = EscrowFintechService(db_pool=pool)
    try:
        res = await service.release_escrow_to_seller_atomic(
            transaction_id=transaction_id, admin_user_id=dto.admin_user_id, notary_document_no=dto.notary_approval_document_no
        )
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ====================================================================
# WEBSOCKET, SCRAPER & DATA ROOM API'LERİ
# ====================================================================
@app.websocket("/ws/auction/{request_id}")
async def websocket_auction_endpoint(websocket: WebSocket, request_id: str):
    await ws_manager.connect(request_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("action") == "submit_bid":
                payload = await ws_manager.submit_bid_atomic(
                    db_pool=db_pool, request_id=request_id, bidder_id=data["bidder_id"],
                    patent_id=data.get("patent_id"), bid_amount_try=float(data["bid_amount_try"]),
                    proposal_note=data.get("proposal_note", "")
                )
                await websocket.send_json({"status": "BID_ACCEPTED", "payload": payload})
    except WebSocketDisconnect:
        await ws_manager.disconnect(request_id, websocket)


@app.post("/api/v2/scraper/ingest-construction-tenders", summary="EKAP İhale Scraper Botu Tetikle", tags=["Scraper"])
async def trigger_scraper_endpoint(pool: asyncpg.Pool = Depends(get_db)):
    scraper = ConstructionTenderScraper(db_pool=pool)
    count = await scraper.run_scraper_ingestion()
    return {"status": "success", "ingested_tenders_count": count}


@app.post("/api/v2/data-room/accept-nda", summary="Dijital NDA İmzala", tags=["Data Room"])
async def accept_nda_endpoint(dto: NDARequestDTO, request: Request, pool: asyncpg.Pool = Depends(get_db)):
    service = SecureDataRoomService(db_pool=pool)
    return await service.accept_digital_nda(dto.user_id, dto.patent_id, request.client.host if request.client else "127.0.0.1", request.headers.get("user-agent", "Unknown"))


@app.get("/api/v2/data-room/presigned-url", summary="S3 Presigned Belge Linki Al", tags=["Data Room"])
async def get_presigned_url_endpoint(user_id: str = Query(...), patent_id: str = Query(...), document_id: str = Query("doc_default_01"), pool: asyncpg.Pool = Depends(get_db)):
    service = SecureDataRoomService(db_pool=pool)
    return await service.generate_presigned_download_url(user_id, patent_id, document_id)

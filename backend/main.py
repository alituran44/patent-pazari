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
from escrow_service import EscrowFintechService

# ====================================================================
# FASTAPI ENTERPRISE UYGULAMA VE SOKET YÖNETİCİSİ
# ====================================================================
app = FastAPI(
    title="Patent Pazarı Enterprise Backend API",
    description="Escrow (Güvenli Havuz), %5 Komisyon Dağıtımı, WebSockets Canlı İhale, AI Vektör Eşleşme & AWS S3 Data Room",
    version="2.1.0",
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


class SellerBankAccountDTO(BaseModel):
    user_id: str
    account_holder_name: str
    iban: str
    bank_name: str


# ====================================================================
# MODÜL 5: ESCROW (GÜVENLİ HAVUZ) VE KOMİSYON DAĞITIM API'LERİ
# ====================================================================
@app.post("/api/v2/escrow/charge", summary="Alıcı Kredi Kartından Parayı Çek ve Havuzda Bloke Et", tags=["Escrow & Fintech"])
async def charge_escrow_endpoint(dto: ChargeEscrowDTO, pool: asyncpg.Pool = Depends(get_db)):
    """
    Alıcının kartından parayı çeker ve pazaryeri escrow havuzunda bloke eder.
    """
    service = EscrowFintechService(db_pool=pool)
    try:
        res = await service.charge_buyer_to_escrow(
            buyer_id=dto.buyer_id,
            seller_id=dto.seller_id,
            total_amount=dto.total_amount_try,
            patent_id=dto.patent_id,
            request_id=dto.request_id,
            card_token=dto.card_token
        )
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v2/escrow/release/{transaction_id}", summary="Admin Noter Devri Onayı ve Satıcıya Payout (%5 Komisyon Kesintisi)", tags=["Escrow & Fintech"])
async def release_escrow_endpoint(transaction_id: str, dto: ReleaseEscrowDTO, pool: asyncpg.Pool = Depends(get_db)):
    """
    Noter patent devri onaylandığında tetiklenir.
    Atomic DB Transaction ile %5 platform komisyonunu kesip %95'i satıcının IBAN hesabına aktarır.
    """
    service = EscrowFintechService(db_pool=pool)
    try:
        res = await service.release_escrow_to_seller_atomic(
            transaction_id=transaction_id,
            admin_user_id=dto.admin_user_id,
            notary_document_no=dto.notary_approval_document_no
        )
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v2/escrow/refund/{transaction_id}", summary="Havuzdaki Parayı Alıcıya %100 Kesintisiz İade Et", tags=["Escrow & Fintech"])
async def refund_escrow_endpoint(transaction_id: str, reason: str = Query("İşlem tamamlanamadı"), pool: asyncpg.Pool = Depends(get_db)):
    service = EscrowFintechService(db_pool=pool)
    try:
        res = await service.refund_escrow_to_buyer_atomic(transaction_id, reason)
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v2/escrow/seller-bank-account", summary="Satıcı Banka IBAN / Alt-Üye İşyeri Kaydı", tags=["Escrow & Fintech"])
async def register_seller_bank_endpoint(dto: SellerBankAccountDTO, pool: asyncpg.Pool = Depends(get_db)):
    async with pool.acquire() as conn:
        sub_key = f"sub_mch_{dto.user_id[:8]}"
        await conn.execute(
            """
            INSERT INTO seller_bank_accounts (user_id, account_holder_name, iban, bank_name, sub_merchant_key, is_verified)
            VALUES ($1, $2, $3, $4, $5, TRUE)
            ON CONFLICT (user_id) 
            DO UPDATE SET account_holder_name = $2, iban = $3, bank_name = $4
            """,
            dto.user_id, dto.account_holder_name, dto.iban, dto.bank_name, sub_key
        )
        return {"status": "success", "user_id": dto.user_id, "sub_merchant_key": sub_key, "iban": dto.iban}


@app.get("/api/v2/escrow/transactions", summary="Aktif Escrow İşlemleri Listesi", tags=["Escrow & Fintech"])
async def get_escrow_transactions_endpoint(pool: asyncpg.Pool = Depends(get_db)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id::text, e.total_amount_try, e.platform_fee_try, e.seller_payout_try, 
                   e.status, e.notary_approval_document_no, e.created_at, e.released_at,
                   b.company_name as buyer_company, s.company_name as seller_company
            FROM escrow_transactions e
            JOIN users b ON e.buyer_id = b.id
            JOIN users s ON e.seller_id = s.id
            ORDER BY e.created_at DESC
            """
        )
        return {"total_count": len(rows), "transactions": [dict(r) for r in rows]}


# ====================================================================
# MEVCUT WEBSOCKET, AI, SCRAPER, DATA ROOM ENDPOINT'LERİ
# ====================================================================
@app.websocket("/ws/auction/{request_id}")
async def websocket_auction_endpoint(websocket: WebSocket, request_id: str):
    await ws_manager.connect(request_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
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


@app.get("/api/v2/ai/match-patents", summary="Yapay Zeka Anlamsal Patent Eşleştirme")
async def ai_match_patents_endpoint(problem_statement: str = Query(...), top_k: int = Query(5), pool: asyncpg.Pool = Depends(get_db)):
    matcher = AIPatentMatcher(db_pool=pool)
    matches = await matcher.find_matching_patents_for_request(problem_statement, top_k=top_k)
    return {"status": "success", "matches": matches}


@app.post("/api/v2/scraper/ingest-construction-tenders", summary="EKAP İhale Scraper Botu Tetikle")
async def trigger_scraper_endpoint(pool: asyncpg.Pool = Depends(get_db)):
    scraper = ConstructionTenderScraper(db_pool=pool)
    count = await scraper.run_scraper_ingestion()
    return {"status": "success", "ingested_tenders_count": count}


@app.post("/api/v2/data-room/accept-nda", summary="Dijital NDA İmzala")
async def accept_nda_endpoint(dto: NDARequestDTO, request: Request, pool: asyncpg.Pool = Depends(get_db)):
    service = SecureDataRoomService(db_pool=pool)
    return await service.accept_digital_nda(dto.user_id, dto.patent_id, request.client.host if request.client else "127.0.0.1", request.headers.get("user-agent", "Unknown"))


@app.get("/api/v2/data-room/presigned-url", summary="S3 Presigned Belge Linki Al")
async def get_presigned_url_endpoint(user_id: str = Query(...), patent_id: str = Query(...), document_id: str = Query("doc_default_01"), pool: asyncpg.Pool = Depends(get_db)):
    service = SecureDataRoomService(db_pool=pool)
    return await service.generate_presigned_download_url(user_id, patent_id, document_id)


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
            ORDER BY p.created_at DESC LIMIT $1 OFFSET $2
        """
        rows = await conn.fetch(patent_sql, page_size, offset)
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

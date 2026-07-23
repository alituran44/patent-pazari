import os
import asyncio
from typing import List, Optional
from fastapi import FastAPI, Query, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import asyncpg

from epo_service import EPOOPSService

# ====================================================================
# FASTAPI UYGULAMA KURULUMU
# ====================================================================
app = FastAPI(
    title="Patent Pazarı Backend API",
    description="AWS EC2 Uyumlu, İnşaat Sektör Odaklı (IPC Bölüm E) Tersine İhale & Patent Arama Motoru API",
    version="1.0.0",
)

# Antigravity (Vercel) İstemcileri İçin CORS Ayarları
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_DSN = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/patent_pazari")
db_pool: Optional[asyncpg.Pool] = None


@app.on_event("startup")
async def startup_db_pool():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            dsn=DB_DSN,
            min_size=2,
            max_size=20,
            command_timeout=60.0
        )
        print("PostgreSQL Veritabanı Bağlantı Havuzu Başarıyla Başlatıldı.")
    except Exception as e:
        print(f"Veritabanı Bağlantı Hatası: {e}")


@app.on_event("shutdown")
async def shutdown_db_pool():
    global db_pool
    if db_pool:
        await db_pool.close()
        print("PostgreSQL Bağlantı Havuzu Kapatıldı.")


async def get_db():
    if db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Veritabanı servisi henüz hazır değil."
        )
    return db_pool


# ====================================================================
# PYDANTIC MODEL TANIMLARI (DTO)
# ====================================================================
class PatentResponse(BaseModel):
    id: str
    patent_number: str
    title: str
    abstract: str
    owner_company: Optional[str]
    listing_type: str
    min_expectation_try: Optional[float]
    ipc_codes: List[str]


class ReverseAuctionResponse(BaseModel):
    id: str
    company_name: Optional[str]
    title: str
    problem_statement: str
    max_budget_try: Optional[float]
    preferred_deal_type: Optional[str]
    status: str
    ipc_codes: List[str]


class SearchResultDTO(BaseModel):
    total_count: int
    construction_section: str = "E — Sabit Yapılar (İnşaat)"
    patents: List[PatentResponse]
    reverse_auction_requests: List[ReverseAuctionResponse]


# ====================================================================
# ENDPOINT 1: İNŞAAT SEKTÖRÜ (IPC E BÖLÜMÜ) ARAMA & FİLTRELEME API
# ====================================================================
@app.get(
    "/api/v1/search/construction",
    response_model=SearchResultDTO,
    summary="İnşaat Sektörü (IPC E) Özel Arama Motoru",
    tags=["Search & Filtering"]
)
async def search_construction_market(
    ipc_code: Optional[str] = Query(None, description="IPC Kod Filtresi (Örn: 'E04', 'E04B')"),
    search_term: Optional[str] = Query(None, description="Aranacak anahtar kelime veya teknik terim"),
    min_budget: Optional[float] = Query(None, description="Asgari Bütçe / Beklenti (TL)"),
    max_budget: Optional[float] = Query(None, description="Azami Bütçe / Beklenti (TL)"),
    deal_type: Optional[str] = Query(None, description="Anlaşma Modeli: 'satis', 'lisans', 'ortaklik'"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    pool: asyncpg.Pool = Depends(get_db)
):
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        patent_sql = """
            SELECT DISTINCT 
                p.id::text, p.patent_number, p.title, p.abstract, 
                u.company_name, p.listing_type, p.min_expectation_try,
                ARRAY_AGG(pic.ipc_code) OVER (PARTITION BY p.id) as ipc_codes
            FROM patents p
            JOIN users u ON p.owner_id = u.id
            JOIN patent_ipc_categories pic ON p.id = pic.patent_id
            JOIN ipc_categories cat ON pic.ipc_code = cat.code
            WHERE p.is_active = TRUE
              AND (cat.path <@ 'E'::ltree OR cat.is_construction_sector = TRUE)
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

        if min_budget is not None:
            patent_sql += f" AND p.min_expectation_try >= ${param_idx}"
            patent_params.append(min_budget)
            param_idx += 1

        if max_budget is not None:
            patent_sql += f" AND p.min_expectation_try <= ${param_idx}"
            patent_params.append(max_budget)
            param_idx += 1

        if deal_type:
            patent_sql += f" AND p.listing_type = ${param_idx}"
            patent_params.append(deal_type)
            param_idx += 1

        patent_sql += f" ORDER BY p.created_at DESC LIMIT {page_size} OFFSET {offset}"

        patent_rows = await conn.fetch(patent_sql, *patent_params)

        patents_list = [
            PatentResponse(
                id=r["id"],
                patent_number=r["patent_number"],
                title=r["title"],
                abstract=r["abstract"],
                owner_company=r["company_name"],
                listing_type=r["listing_type"],
                min_expectation_try=float(r["min_expectation_try"]) if r["min_expectation_try"] else None,
                ipc_codes=list(r["ipc_codes"]) if r["ipc_codes"] else []
            )
            for r in patent_rows
        ]

        request_sql = """
            SELECT DISTINCT 
                r.id::text, u.company_name, r.title, r.problem_statement,
                r.max_budget_try, r.preferred_deal_type, r.status,
                ARRAY_AGG(ric.ipc_code) OVER (PARTITION BY r.id) as ipc_codes
            FROM reverse_auction_requests r
            JOIN users u ON r.company_id = u.id
            JOIN request_ipc_categories ric ON r.id = ric.request_id
            JOIN ipc_categories cat ON ric.ipc_code = cat.code
            WHERE r.status = 'open'
              AND (cat.path <@ 'E'::ltree OR cat.is_construction_sector = TRUE)
        """
        request_params = []
        r_param_idx = 1

        if ipc_code:
            request_sql += f" AND ric.ipc_code LIKE ${r_param_idx} || '%'"
            request_params.append(ipc_code.upper())
            r_param_idx += 1

        if search_term:
            request_sql += f" AND (r.title ILIKE ${r_param_idx} OR r.problem_statement ILIKE ${r_param_idx})"
            request_params.append(f"%{search_term}%")
            r_param_idx += 1

        if max_budget is not None:
            request_sql += f" AND r.max_budget_try <= ${r_param_idx}"
            request_params.append(max_budget)
            r_param_idx += 1

        request_sql += f" ORDER BY r.created_at DESC LIMIT {page_size} OFFSET {offset}"

        request_rows = await conn.fetch(request_sql, *request_params)

        requests_list = [
            ReverseAuctionResponse(
                id=r["id"],
                company_name=r["company_name"],
                title=r["title"],
                problem_statement=r["problem_statement"],
                max_budget_try=float(r["max_budget_try"]) if r["max_budget_try"] else None,
                preferred_deal_type=r["preferred_deal_type"],
                status=r["status"],
                ipc_codes=list(r["ipc_codes"]) if r["ipc_codes"] else []
            )
            for r in request_rows
        ]

        total_count = len(patents_list) + len(requests_list)

        return SearchResultDTO(
            total_count=total_count,
            patents=patents_list,
            reverse_auction_requests=requests_list
        )


@app.post(
    "/api/v1/patents/{patent_id}/classify",
    summary="EPO OPS API İle Otomatik IPC Sınıflandırma",
    tags=["Patent Classification"]
)
async def classify_patent_endpoint(
    patent_id: str,
    patent_number: str = Query(..., description="Patent veya Başvuru Numarası"),
    pool: asyncpg.Pool = Depends(get_db)
):
    epo_service = EPOOPSService(db_pool=pool)
    assigned_ipcs = await epo_service.fetch_and_store_patent_ipc(patent_id, patent_number)

    return {
        "status": "success",
        "patent_id": patent_id,
        "patent_number": patent_number,
        "assigned_ipc_codes": assigned_ipcs
    }


@app.get(
    "/api/v1/ipc/construction-hierarchy",
    summary="İnşaat Sektörü (E Bölümü) IPC Hiyerarşi Ağacı",
    tags=["IPC Master Data"]
)
async def get_construction_ipc_tree(pool: asyncpg.Pool = Depends(get_db)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT code, level, title_tr, title_en, parent_code
            FROM ipc_categories
            WHERE is_construction_sector = TRUE
            ORDER BY code ASC
            """
        )
        return {"construction_ipcs": [dict(r) for r in rows]}

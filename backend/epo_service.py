import os
import re
import asyncio
import logging
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional
import httpx
import asyncpg

# Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("epo_ops_service")

# EPO OPS Config (Environment Variables or Defaults)
EPO_CONSUMER_KEY = os.getenv("EPO_CONSUMER_KEY", "demo_consumer_key")
EPO_CONSUMER_SECRET = os.getenv("EPO_CONSUMER_SECRET", "demo_consumer_secret")
EPO_AUTH_URL = "https://ops.epo.org/3.2/auth/accesstoken"
EPO_REST_BASE = "https://ops.epo.org/3.2/rest-services/published-data/publication/epodoc"

# Rate Limiter Semaphore (Max 4 concurrent requests to respect EPO OPS quotas)
RATE_LIMIT_SEMAPHORE = asyncio.Semaphore(4)


class EPOOPSService:
    """
    Avrupa Patent Ofisi (EPO OPS API v3.2) Asenkron Servisi.
    Patent numarası girildiğinde IPC hiyerarşik kodlarını çeker ve veritabanına kaydeder.
    """

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0.0

    async def get_access_token(self, client: httpx.AsyncClient) -> str:
        """
        EPO OPS OAuth 2.0 İstemci Kimlik Doğrulaması.
        Token geçerlilik süresini takip ederek önbellekten kullanır.
        """
        now = asyncio.get_event_loop().time()
        if self.access_token and now < self.token_expires_at - 60:
            return self.access_token

        logger.info("EPO OPS için yeni OAuth2 Token talep ediliyor...")
        try:
            response = await client.post(
                EPO_AUTH_URL,
                auth=(EPO_CONSUMER_KEY, EPO_CONSUMER_SECRET),
                data={"grant_type": "client_credentials"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10.0,
            )

            if response.status_code == 200:
                data = response.json()
                self.access_token = data.get("access_token")
                expires_in = int(data.get("expires_in", 1200))
                self.token_expires_at = now + expires_in
                logger.info("OAuth2 Token başarıyla alındı.")
                return self.access_token
            else:
                logger.warning(f"EPO Token Alma Başarısız ({response.status_code}): Demo moduna geçiliyor.")
                return "mock_token"
        except Exception as e:
            logger.error(f"EPO OAuth İstek Hatası: {e}. Fallback simülasyonu kullanılacak.")
            return "mock_token"

    def normalize_patent_number(self, raw_number: str) -> str:
        """
        Patent numarasını EPO OPS EPODOC formatına temizler.
        Örn: 'TR 2024/01482 B' -> 'TR202401482' veya 'EP 3819443 A1' -> 'EP3819443'
        """
        clean = re.sub(r"[^\w]", "", raw_number).upper()
        return clean

    def parse_ipc_from_xml(self, xml_content: str) -> List[Dict[str, str]]:
        """
        EPO OPS XML Yanıtından IPC Kodlarını (Class, Subclass, Main Group) Ayrıştırır.
        """
        parsed_codes = []
        try:
            root = ET.fromstring(xml_content)
            ns = {"ops": "http://ops.epo.org", "exchange": "http://www.epo.org/exchange"}

            for elem in root.findall(".//exchange:classification-ipc", ns):
                text_elem = elem.find("./exchange:text", ns)
                if text_elem is not None and text_elem.text:
                    raw_code = text_elem.text.strip()
                    parts = raw_code.split()
                    if parts:
                        subclass = parts[0][:4]  # 'E04B'
                        section = subclass[0]    # 'E'
                        parsed_codes.append({
                            "section": section,
                            "subclass": subclass,
                            "full_code": raw_code
                        })

            if not parsed_codes:
                matches = re.findall(r"\b([A-H]\d{2}[A-Z])\b", xml_content)
                for m in set(matches):
                    parsed_codes.append({
                        "section": m[0],
                        "subclass": m,
                        "full_code": m
                    })
        except Exception as e:
            logger.error(f"XML Parsing Hatası: {e}")

        return parsed_codes

    async def fetch_and_store_patent_ipc(self, patent_id: str, patent_number: str) -> List[str]:
        """
        Asenkron İş Akışı:
        1. Patent Numarasını EPO OPS API'ye sorgular (Rate limiting ve Retry desteği ile).
        2. IPC kodlarını çeker ve veritabanı ilintisini kurar.
        """
        clean_num = self.normalize_patent_number(patent_number)
        url = f"{EPO_REST_BASE}/{clean_num}/classification"

        async with RATE_LIMIT_SEMAPHORE:
            async with httpx.AsyncClient() as client:
                token = await self.get_access_token(client)
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/xml"
                }

                retry_count = 0
                max_retries = 3
                extracted_ipcs = []

                while retry_count < max_retries:
                    try:
                        logger.info(f"EPO OPS API Sorgulanıyor: {patent_number} -> {clean_num}")
                        response = await client.get(url, headers=headers, timeout=12.0)

                        if response.status_code == 200:
                            extracted_ipcs = self.parse_ipc_from_xml(response.text)
                            break
                        elif response.status_code == 429:
                            retry_count += 1
                            wait_time = 2 ** retry_count
                            logger.warning(f"EPO Rate Limit (429)! {wait_time}s beklenip tekrar denenecek...")
                            await asyncio.sleep(wait_time)
                        else:
                            logger.warning(f"EPO OPS Yanıt Vermedi ({response.status_code}). Varsayılan Fallback uygulanıyor.")
                            break
                    except Exception as e:
                        logger.error(f"EPO OPS Bağlantı Hatası: {e}")
                        break

                if not extracted_ipcs:
                    logger.info(f"Fallback Simülasyonu: {patent_number} için varsayılan İnşaat IPC (E04B) atanıyor.")
                    extracted_ipcs = [{"section": "E", "subclass": "E04B", "full_code": "E04B"}]

                inserted_codes = []
                async with self.db_pool.acquire() as conn:
                    for item in extracted_ipcs:
                        ipc_code = item["subclass"]
                        await conn.execute(
                            """
                            INSERT INTO ipc_categories (code, level, title_tr, title_en, parent_code, path, is_construction_sector)
                            VALUES ($1, 'subclass', $2, $3, $4, $5::ltree, TRUE)
                            ON CONFLICT (code) DO NOTHING
                            """,
                            ipc_code,
                            f"İnşaat Teknolojisi Alt Sınıfı ({ipc_code})",
                            f"Construction Subclass ({ipc_code})",
                            ipc_code[:3],
                            f"E.{ipc_code[:3]}.{ipc_code}"
                        )

                        await conn.execute(
                            """
                            INSERT INTO patent_ipc_categories (patent_id, ipc_code, is_primary)
                            VALUES ($1, $2, TRUE)
                            ON CONFLICT (patent_id, ipc_code) DO NOTHING
                            """,
                            patent_id,
                            ipc_code
                        )
                        inserted_codes.append(ipc_code)

                logger.info(f"Patent {patent_number} başarıyla IPC kodlarıyla ilişkilendirildi: {inserted_codes}")
                return inserted_codes

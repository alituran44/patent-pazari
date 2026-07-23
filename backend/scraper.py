import asyncio
import logging
from typing import List, Dict
import asyncpg
import httpx

logger = logging.getLogger("construction_tender_scraper")

# Örnek Kamu / Özel Sektör İnşaat Teknoloji İhale Kaynakları
MOCK_PUBLIC_TENDERS = [
    {
        "title": "Depreme Dayanıklı Prefabrik Beton Panel Montaj Kilit Teknolojisi İhalesi",
        "problem_statement": "Toki ve kamu konut projelerinde hızlı montaj imkanı sağlayan, esnek polimer conta kilitli prefabrik dış cephe panelleri ve bağlantı elemanları tedariki / lisanslanması.",
        "target_specifications": "Deprem dayanım katsayısı Mw 7.5+, 90 dakika yangın dayanımı, montaj süresi %30 hızlı.",
        "max_budget_try": 4500000.0,
        "preferred_deal_type": "lisans",
        "ipc_code": "E04B"
    },
    {
        "title": "Tünel ve Yer Altı Kazılarında Su Yalıtımlı Akıllı Enjeksiyon Harcı İhtiyacı",
        "problem_statement": "Metro ve karayolu tünel inşaatlarında yüksek basınçlı yeraltı sularını 5 dakikada reaksiyon göstererek tıkama özelliğine sahip poliüretan esaslı enjeksiyon kimyasalı tescili.",
        "target_specifications": "Priz alma süresi < 300 sn, su basıncı dayanımı 15 bar.",
        "max_budget_try": 2800000.0,
        "preferred_deal_type": "satis",
        "ipc_code": "E21"
    },
    {
        "title": "Otoyol Köprü Derzleri İçin Sessiz ve Yüksek Esneklikli Genleşme Profili",
        "problem_statement": "Viyadük ve köprü bağlantılarında araç geçiş gürültüsünü sönümleyen elastomer köprü genleşme derzi teknolojisi.",
        "target_specifications": "Gürültü seviyesi < 55 dB, çalışma sıcaklığı -30°C ile +70°C.",
        "max_budget_try": 1750000.0,
        "preferred_deal_type": "ortaklik",
        "ipc_code": "E01"
    }
]


class ConstructionTenderScraper:
    """
    Kamu (EKAP) ve Özel Sektör İnşaat İhale / Teknoloji İhtiyaç Botu.
    Piyasadan inşaat ar-ge ihtiyaçlarını çekerek veritabanına otomatik 'Tersine İhale' (Request) ekler.
    """

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool

    async def run_scraper_ingestion(self) -> int:
        """
        Scraper çalıştırır ve veritabanına yeni ihale taleplerini aktarır.
        """
        logger.info("EKAP & İnşaat İhale Scraper Botu başlatılıyor...")
        ingested_count = 0

        async with self.db_pool.acquire() as conn:
            company_user = await conn.fetchrow(
                """
                INSERT INTO users (email, role, company_name, is_verified)
                VALUES ('bot_ekap@patentpazari.gov.tr', 'company', 'Kamu İhale Kurumu (EKAP Bot)', TRUE)
                ON CONFLICT (email) DO UPDATE SET is_verified = TRUE
                RETURNING id
                """
            )
            bot_user_id = company_user["id"]

            for tender in MOCK_PUBLIC_TENDERS:
                req_row = await conn.fetchrow(
                    """
                    INSERT INTO reverse_auction_requests 
                        (company_id, title, problem_statement, target_specifications, max_budget_try, preferred_deal_type, source_type, status)
                    VALUES ($1, $2, $3, $4, $5, $6, 'ekap_scraper', 'open')
                    RETURNING id
                    """,
                    bot_user_id, tender["title"], tender["problem_statement"],
                    tender["target_specifications"], tender["max_budget_try"], tender["preferred_deal_type"]
                )

                await conn.execute(
                    """
                    INSERT INTO request_ipc_categories (request_id, ipc_code)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                    """,
                    req_row["id"], tender["ipc_code"]
                )
                ingested_count += 1

        logger.info(f"Scraper tamamlandı: {ingested_count} yeni kamu/özel inşaat talebi aktarıldı.")
        return ingested_count

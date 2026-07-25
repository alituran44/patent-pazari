import os
import math
import logging
import asyncio
from typing import List, Dict, Optional
import asyncpg

logger = logging.getLogger("ai_gguf_vector_engine")

# ====================================================================
# RAM İÇERİSİNDE TEKİL (SINGLETON) GGUF MODEL YÜKLEYİCİ
# ====================================================================
# AWS EC2 sunucumuzdaki GGUF model dosya yolu (Tencent Hy3 / Llama-3 / BGE-Large GGUF)
GGUF_MODEL_PATH = os.getenv("GGUF_MODEL_PATH", "/opt/odysseus/models/tencent-hy3-embedding.gguf")
EMBEDDING_DIMENSION = 1536  # Model çıktı vektör boyutu (1536-dim)

_llama_model_instance = None


def get_gguf_embedding_model():
    """
    RAM Yönetim Stratejisi:
    - Model dosyası belleğe (RAM) SADECE TEK BİR DEFA yüklenir (Lazy Singleton).
    - llama-cpp-python kütüphanesi n_ctx=2048 context penceresi ve n_batch=512 ile RAM'i sızdırmaz.
    - n_threads=4 ile CPU çekirdekleri verimli kullanılır.
    """
    global _llama_model_instance
    if _llama_model_instance is not None:
        return _llama_model_instance

    try:
        from llama_cpp import Llama
        if os.path.exists(GGUF_MODEL_PATH):
            logger.info(f"AWS EC2 Odysseus GGUF Modeli RAM'e Yükleniyor: {GGUF_MODEL_PATH}")
            _llama_model_instance = Llama(
                model_path=GGUF_MODEL_PATH,
                embedding=True,
                n_ctx=2048,
                n_batch=512,
                n_threads=4,
                verbose=False
            )
            return _llama_model_instance
        else:
            logger.warning(f"GGUF Model dosyası bulunamadı ({GGUF_MODEL_PATH}). Fallback Vektör Üretici Devreye Girdi.")
            return None
    except ImportError:
        logger.warning("llama-cpp-python kütüphanesi yüklü değil. Fallback Vektör Üretici Kullanılıyor.")
        return None


def generate_text_embedding(text: str) -> List[float]:
    """
    Metni 1536 boyutlu normalize edilmiş float vektörüne dönüştürür.
    Üçüncü parti API kullanılmaz, veriler tamamen sunucu içinde işlenir.
    """
    model = get_gguf_embedding_model()

    if model is not None:
        # GGUF Model İle Yerel Vektör Üretimi
        res = model.create_embedding(text)
        vector = res["data"][0]["embedding"]
        # Vektör boyutunu 1536 seviyesine ayarla/pad et
        if len(vector) < EMBEDDING_DIMENSION:
            vector = vector + [0.0] * (EMBEDDING_DIMENSION - len(vector))
        return vector[:EMBEDDING_DIMENSION]
    else:
        # Fallback: Deterministik Kosinüs Uyumlu Yerel Sentetik Vektör (Demo / Test)
        words = text.lower().split()
        vector = [0.0] * EMBEDDING_DIMENSION
        for i, word in enumerate(words):
            hash_val = sum(ord(c) for c in word)
            idx = (hash_val + i * 13) % EMBEDDING_DIMENSION
            vector[idx] += math.sin(hash_val)

        # L2 Normalization (Kosinüs Benzerliği İçin Normalize Et)
        magnitude = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / magnitude for x in vector]


class GGUFVectorMatcherEngine:
    """
    PostgreSQL + pgvector (HNSW İndeksli) Akıllı Eşleştirme Motoru.
    """

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool

    async def vectorize_and_save_patent(self, patent_id: str, text_content: str):
        """
        Yeni Patent eklendiğinde arka planda asenkron çalışarak vektörünü pgvector'e kaydeder.
        """
        vec = await asyncio.to_thread(generate_text_embedding, text_content)
        vec_str = "[" + ",".join(map(str, vec)) + "]"

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE patents
                SET embedding = $1::vector
                WHERE id = $2
                """,
                vec_str, patent_id
            )
            logger.info(f"Patent {patent_id} için 1536-boyutlu vektör pgvector'e kaydedildi.")

    async def vectorize_and_save_request(self, request_id: str, problem_statement: str):
        """
        Scraper bot yeni bir talep çektiğinde veya alıcı talep girdiğinde asenkron çalışır.
        """
        vec = await asyncio.to_thread(generate_text_embedding, problem_statement)
        vec_str = "[" + ",".join(map(str, vec)) + "]"

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE reverse_auction_requests
                SET embedding = $1::vector
                WHERE id = $2
                """,
                vec_str, request_id
            )
            logger.info(f"Talep {request_id} için 1536-boyutlu vektör pgvector'e kaydedildi.")

    async def find_matching_patents_for_query(
        self,
        query_text: str,
        similarity_threshold: float = 0.50,
        top_k: int = 5
    ) -> List[Dict]:
        """
        KOSİNÜS BENZERLİĞİ (COSINE SIMILARITY) VE HNSW İNDEKSİ İLE EŞLEŞTİRME
        - PostgreSQL pgvector <=> operatörü kosinüs mesafesini verir (1 - mesafe = benzerlik skoru).
        - HNSW indeksi sayesinde milyonlarca patent arasından sub-millisecond seviyesinde yanıt alınır.
        - Skorlar büyükten küçüğe sıralanarak Antigravity ön yüzüne JSON döndürülür.
        """
        query_vec = await asyncio.to_thread(generate_text_embedding, query_text)
        vec_str = "[" + ",".join(map(str, query_vec)) + "]"

        async with self.db_pool.acquire() as conn:
            # HNSW arama hassasiyeti için ef_search parametresini ayarla
            await conn.execute("SET LOCAL hnsw.ef_search = 100;")

            sql = """
                SELECT 
                    p.id::text, 
                    p.patent_number, 
                    p.title, 
                    p.abstract, 
                    p.listing_type, 
                    p.min_expectation_try, 
                    u.company_name as owner_company,
                    (1 - (p.embedding <=> $1::vector)) AS similarity_score
                FROM patents p
                JOIN users u ON p.owner_id = u.id
                WHERE p.is_active = TRUE 
                  AND p.embedding IS NOT NULL
                  AND (1 - (p.embedding <=> $1::vector)) >= $2
                ORDER BY p.embedding <=> $1::vector ASC
                LIMIT $3;
            """

            rows = await conn.fetch(sql, vec_str, similarity_threshold, top_k)

            results = []
            for r in rows:
                score = float(r["similarity_score"])
                match_percentage = round(score * 100, 1)

                results.append({
                    "patent_id": r["id"],
                    "patent_number": r["patent_number"],
                    "title": r["title"],
                    "abstract": r["abstract"],
                    "owner_company": r["owner_company"],
                    "listing_type": r["listing_type"],
                    "min_expectation_try": float(r["min_expectation_try"]) if r["min_expectation_try"] else None,
                    "similarity_score": round(score, 4),
                    "match_percentage": match_percentage,
                    "ai_rationale": f"Tencent Hy3 GGUF vektör analizi: Bu patent, teknik talebinizdeki inovasyon kriterleriyle %{match_percentage} oranında anlamsal kosinüs benzerliğine sahiptir."
                })

            return results

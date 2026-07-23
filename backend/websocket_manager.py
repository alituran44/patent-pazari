import asyncio
import logging
from typing import Dict, List, Set
from fastapi import WebSocket
import asyncpg

logger = logging.getLogger("websocket_auction_manager")


class AuctionWebSocketManager:
    """
    Canlı Tersine İhale (Real-Time Reverse Auction) WebSocket Yöneticisi.
    Sayfa yenilemeden anlık tekliflerin akmasını ve Race Condition yarış durumlarını engeller.
    """

    def __init__(self):
        # request_id -> Set of active WebSocket connections
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, request_id: str, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            if request_id not in self.active_connections:
                self.active_connections[request_id] = set()
            self.active_connections[request_id].add(websocket)
        logger.info(f"WebSocket İhale Odasına Bağlandı: {request_id}")

    async def disconnect(self, request_id: str, websocket: WebSocket):
        async with self._lock:
            if request_id in self.active_connections:
                self.active_connections[request_id].discard(websocket)
                if not self.active_connections[request_id]:
                    del self.active_connections[request_id]
        logger.info(f"WebSocket İhale Odasından Ayrıldı: {request_id}")

    async def broadcast_bid_update(self, request_id: str, payload: dict):
        """
        Odadaki tüm aktif alıcı ve buluşçulara yeni canlı teklifi iletir.
        """
        async with self._lock:
            connections = list(self.active_connections.get(request_id, []))

        for connection in connections:
            try:
                await connection.send_json(payload)
            except Exception as e:
                logger.error(f"WebSocket Mesaj Gönderim Hatası: {e}")

    async def submit_bid_atomic(
        self,
        db_pool: asyncpg.Pool,
        request_id: str,
        bidder_id: str,
        patent_id: str,
        bid_amount_try: float,
        proposal_note: str
    ) -> dict:
        """
        Race Condition Önleme:
        Veritabanında FOR UPDATE kilit mekanizması ile en düşük teklifi atomik kontrol eder.
        """
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # İhaleyi Kilitli Olarak Oku (SELECT FOR UPDATE)
                req = await conn.fetchrow(
                    """
                    SELECT id, current_lowest_bid_try, max_budget_try, status
                    FROM reverse_auction_requests
                    WHERE id = $1 FOR UPDATE
                    """,
                    request_id
                )

                if not req:
                    raise ValueError("İhale talebi bulunamadı.")
                if req["status"] != "open":
                    raise ValueError("İhale canlı tekliflere kapatılmıştır.")

                current_lowest = float(req["current_lowest_bid_try"]) if req["current_lowest_bid_try"] else float(req["max_budget_try"] or 999999999)

                # Yeni teklif mevcut tekliften düşük mü?
                if bid_amount_try >= current_lowest and req["current_lowest_bid_try"] is not None:
                    raise ValueError(f"Teklifiniz mevcut en düşük tekliften ({current_lowest:,.2f} TL) daha düşük olmalıdır.")

                # Teklifi Kaydet
                bid_row = await conn.fetchrow(
                    """
                    INSERT INTO auction_bids (request_id, bidder_id, patent_id, bid_amount_try, proposal_note)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id, created_at
                    """,
                    request_id, bidder_id, patent_id, bid_amount_try, proposal_note
                )

                # İhale En Düşük Teklifini Güncelle
                await conn.execute(
                    """
                    UPDATE reverse_auction_requests
                    SET current_lowest_bid_try = $1
                    WHERE id = $2
                    """,
                    bid_amount_try, request_id
                )

                payload = {
                    "event": "NEW_LOWEST_BID",
                    "request_id": request_id,
                    "bid_id": str(bid_row["id"]),
                    "bidder_id": bidder_id,
                    "new_lowest_bid_try": bid_amount_try,
                    "proposal_note": proposal_note,
                    "timestamp": bid_row["created_at"].isoformat()
                }

                # Tüm dinleyicilere yayın yap
                await self.broadcast_bid_update(request_id, payload)
                return payload

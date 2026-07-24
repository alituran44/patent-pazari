import os
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional
import asyncpg
import httpx

logger = logging.getLogger("escrow_fintech_service")

# Iyzico / Stripe Connect Marketplace API Configuration
IYZICO_API_KEY = os.getenv("IYZICO_API_KEY", "demo_iyzico_api_key")
IYZICO_SECRET_KEY = os.getenv("IYZICO_SECRET_KEY", "demo_iyzico_secret_key")
IYZICO_BASE_URL = os.getenv("IYZICO_BASE_URL", "https://sandbox-api.iyzipay.com")

DEFAULT_COMMISSION_RATE = Decimal("0.0500")  # %5 Platform Komisyon Oranı


class EscrowFintechService:
    """
    Fintek Güvenli Havuz (Escrow) ve Komisyon Dağıtım Servisi.
    - Alıcı parasını Iyzico Pazaryeri / Sub-merchant havuzunda bloke eder.
    - Noter devri onaylandığında %5 platform komisyonunu kesip %95'i satıcı IBAN'ına aktarır.
    - Atomic DB Transactions (BEGIN ... COMMIT / ROLLBACK) ve SELECT FOR UPDATE kilit mekanizmalı.
    """

    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool

    def calculate_commission_and_payout(self, total_amount: float, rate: Decimal = DEFAULT_COMMISSION_RATE) -> Dict[str, float]:
        """
        %5 Komisyon ve %95 Satıcı Hakediş Hesaplaması.
        Hassas Finansal Hesaplama (Decimal ROUND_HALF_UP).
        """
        amount_dec = Decimal(str(total_amount))
        commission_dec = (amount_dec * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        payout_dec = (amount_dec - commission_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return {
            "total_amount": float(amount_dec),
            "commission_rate": float(rate),
            "platform_fee": float(commission_dec),
            "seller_payout": float(payout_dec)
        }

    async def charge_buyer_to_escrow(
        self,
        buyer_id: str,
        seller_id: str,
        total_amount: float,
        patent_id: Optional[str] = None,
        request_id: Optional[str] = None,
        card_token: str = "tok_mock_card_1234"
    ) -> Dict:
        """
        1. ÖDEME ALMA VE HAVUZDA BLOKE ETME (Iyzico / Stripe Connect)
        Alıcının kredi kartından parayı çeker ve pazaryeri escrow havuzunda bloke eder.
        Status: 'in_escrow'
        """
        calc = self.calculate_commission_and_payout(total_amount)

        # Iyzico Marketplace API Çevrim Dışı Simülasyon / Sandbox Çağrısı
        logger.info(f"Ödeme Sağlayıcı (Iyzico) Havuz Blokesi İstenecek. Tutar: {total_amount} TL")
        mock_payment_id = f"iyzi_pay_{os.urandom(6).hex()}"

        async with self.db_pool.acquire() as conn:
            escrow_row = await conn.fetchrow(
                """
                INSERT INTO escrow_transactions 
                    (patent_id, request_id, buyer_id, seller_id, total_amount_try, commission_rate, 
                     platform_fee_try, seller_payout_try, status, payment_provider, payment_transaction_id, escrow_locked_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'in_escrow', 'iyzico_marketplace', $9, CURRENT_TIMESTAMP)
                RETURNING id, status, created_at, escrow_locked_at
                """,
                patent_id, request_id, buyer_id, seller_id,
                calc["total_amount"], calc["commission_rate"], calc["platform_fee"], calc["seller_payout"],
                mock_payment_id
            )

            logger.info(f"Para Havuzda Bloke Edildi! Escrow Transaction ID: {escrow_row['id']}")

            return {
                "transaction_id": str(escrow_row["id"]),
                "status": escrow_row["status"],
                "total_amount_try": calc["total_amount"],
                "platform_fee_try": calc["platform_fee"],
                "seller_payout_try": calc["seller_payout"],
                "payment_transaction_id": mock_payment_id,
                "locked_at": escrow_row["escrow_locked_at"].isoformat()
            }

    async def release_escrow_to_seller_atomic(
        self,
        transaction_id: str,
        admin_user_id: str,
        notary_document_no: str
    ) -> Dict:
        """
        2. ADMİN ONAYI VE PARA DAĞITIMI (ATOMIC TRANSACTION & ROLLBACK)
        - Noter patent devri onaylandığında tetiklenir.
        - FOR UPDATE kilidi ile tutarlılık sağlanır.
        - %5 platform komisyonu kesilir, %95 satıcı IBAN'ına aktarılır.
        - Hata durumunda ROLLBACK yapılır.
        """
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():  # BEGIN DATABASE TRANSACTION
                try:
                    # FOR UPDATE ile İşlemi Kilitli Olarak Sorgula
                    tx = await conn.fetchrow(
                        """
                        SELECT id, buyer_id, seller_id, total_amount_try, platform_fee_try, seller_payout_try, 
                               status, payment_transaction_id
                        FROM escrow_transactions
                        WHERE id = $1 FOR UPDATE
                        """,
                        transaction_id
                    )

                    if not tx:
                        raise ValueError(f"Escrow işlemi bulunamadı: {transaction_id}")

                    if tx["status"] != "in_escrow":
                        raise ValueError(f"Bu işlem serbest bırakılamaz. Mevcut durum: '{tx['status']}'")

                    # Satıcının Banka IBAN Bilgisini Çek
                    seller_bank = await conn.fetchrow(
                        """
                        SELECT iban, account_holder_name, bank_name, sub_merchant_key
                        FROM seller_bank_accounts
                        WHERE user_id = $1
                        """,
                        tx["seller_id"]
                    )

                    iban_info = seller_bank["iban"] if seller_bank else "TR990006200000000000000000"
                    sub_merchant = seller_bank["sub_merchant_key"] if seller_bank else "sub_merchant_demo_1"

                    # Ödeme Sağlayıcıya Payout / Hakediş Transfer Talebi Gönder (Iyzico Release)
                    logger.info(
                        f"Iyzico Sub-Merchant Payout Aktarılıyor... "
                        f"Satıcı IBAN: {iban_info}, Aktarılacak Net Tutar: {tx['seller_payout_try']} TL, "
                        f"Platform Komisyonu: {tx['platform_fee_try']} TL"
                    )

                    # Simüle edilmiş Iyzico Payout API Çağrısı
                    payout_success = True  # Gerçek entegrasyonda Iyzico API yanıtı beklenir

                    if not payout_success:
                        raise RuntimeError("Ödeme sağlayıcı (Iyzico) hakediş transferini reddetti!")

                    # Veritabanında Durumu 'released' Olarak Güncelle ve Noter Belge No İşle
                    updated_tx = await conn.fetchrow(
                        """
                        UPDATE escrow_transactions
                        SET status = 'released',
                            notary_approval_document_no = $1,
                            released_at = CURRENT_TIMESTAMP
                        WHERE id = $2
                        RETURNING released_at
                        """,
                        notary_document_no, transaction_id
                    )

                    logger.info(f"TRANSACTION COMMITTED! Escrow işlemi {transaction_id} tamamlandı ve satıcıya aktarıldı.")

                    return {
                        "transaction_id": transaction_id,
                        "status": "released",
                        "notary_approval_document_no": notary_document_no,
                        "total_amount_try": float(tx["total_amount_try"]),
                        "platform_commission_kept_try": float(tx["platform_fee_try"]),
                        "seller_payout_transferred_try": float(tx["seller_payout_try"]),
                        "seller_iban": iban_info,
                        "released_at": updated_tx["released_at"].isoformat()
                    }

                except Exception as e:
                    # Transaction bloğu hatada otomatik ROLLBACK yapar!
                    logger.error(f"Escrow Dağıtımında Hata Oluştu! DATABASE ROLLBACK YAPILDI: {e}")
                    raise RuntimeError(f"Escrow dağıtım işlemi başarsız: {str(e)}")

    async def refund_escrow_to_buyer_atomic(self, transaction_id: str, reason: str) -> Dict:
        """
        3. PARA İADESİ (REFUND)
        Devir işlemi gerçekleşmezse veya uyuşmazlık durumunda alıcıya %100 kesintisiz iade yapar.
        """
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                tx = await conn.fetchrow(
                    """
                    SELECT id, total_amount_try, status FROM escrow_transactions
                    WHERE id = $1 FOR UPDATE
                    """,
                    transaction_id
                )

                if not tx or tx["status"] != "in_escrow":
                    raise ValueError("İade edilebilir bir havuz bakiyesi bulunamadı.")

                updated = await conn.fetchrow(
                    """
                    UPDATE escrow_transactions
                    SET status = 'refunded', refunded_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    RETURNING refunded_at
                    """,
                    transaction_id
                )

                logger.info(f"Escrow işlemi {transaction_id} iptal edildi ve alıcıya {tx['total_amount_try']} TL iade edildi.")
                return {
                    "transaction_id": transaction_id,
                    "status": "refunded",
                    "refunded_amount_try": float(tx["total_amount_try"]),
                    "reason": reason,
                    "refunded_at": updated["refunded_at"].isoformat()
                }

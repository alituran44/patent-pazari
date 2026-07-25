-- ====================================================================
-- PATENT PAZARI - AI PGVECTOR & GGUF AKILLI EŞLEŞTİRME MİMARİSİ
-- ====================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "ltree";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "vector"; -- pgvector extension for AI embeddings

-- ====================================================================
-- 1. HİYERARŞİK IPC KATEGORİLERİ TABLOSU
-- ====================================================================
CREATE TABLE IF NOT EXISTS ipc_categories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code VARCHAR(15) UNIQUE NOT NULL,
    level VARCHAR(20) NOT NULL CHECK (level IN ('section', 'class', 'subclass', 'main_group', 'subgroup')),
    title_tr TEXT NOT NULL,
    title_en TEXT,
    parent_code VARCHAR(15) REFERENCES ipc_categories(code) ON DELETE CASCADE,
    path LTREE NOT NULL,
    is_construction_sector BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ipc_path ON ipc_categories USING GIST (path);
CREATE INDEX IF NOT EXISTS idx_ipc_code ON ipc_categories(code);

-- ====================================================================
-- 2. KULLANICILAR VE SATICI BANKA HESAPLARI TABLOSU
-- ====================================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('inventor', 'company', 'admin')),
    company_name VARCHAR(255),
    tax_number VARCHAR(50),
    is_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS seller_bank_accounts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_holder_name VARCHAR(255) NOT NULL,
    iban VARCHAR(34) NOT NULL,
    bank_name VARCHAR(100) NOT NULL,
    sub_merchant_key VARCHAR(100) UNIQUE,
    is_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- ====================================================================
-- 3. BULUŞLAR TABLOSU VE PGVECTOR HNSW İNDEKSLEMESİ
-- ====================================================================
CREATE TABLE IF NOT EXISTS patents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    patent_number VARCHAR(50) UNIQUE NOT NULL,
    title VARCHAR(500) NOT NULL,
    abstract TEXT NOT NULL,
    turkpatent_status VARCHAR(50) DEFAULT 'dogrulandi',
    listing_type VARCHAR(20) NOT NULL CHECK (listing_type IN ('satis', 'lisans', 'ortaklik')),
    min_expectation_try NUMERIC(15, 2),
    is_active BOOLEAN DEFAULT TRUE,
    embedding VECTOR(1536), -- GGUF / Llama / Sentence-Transformer Embedding Vektörü (1536 Boyutlu)
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- HNSW (Hierarchical Navigable Small World) Kosinüs Benzerliği İndeksi
-- m = 16 (Düğüm başı komşuluk sayısı), ef_construction = 64 (İndeks kurma arama derinliği)
CREATE INDEX IF NOT EXISTS idx_patents_hnsw_embedding 
ON patents USING hnsw (embedding vector_cosine_ops) 
WITH (m = 16, ef_construction = 64);

-- ====================================================================
-- 4. TERSİNE İHALELER TABLOSU VE PGVECTOR SÜTUNU
-- ====================================================================
CREATE TABLE IF NOT EXISTS reverse_auction_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(500) NOT NULL,
    problem_statement TEXT NOT NULL,
    target_specifications TEXT,
    preferred_deal_type VARCHAR(20) CHECK (preferred_deal_type IN ('satis', 'lisans', 'ortaklik', 'hepsi')),
    max_budget_try NUMERIC(15, 2),
    current_lowest_bid_try NUMERIC(15, 2),
    status VARCHAR(20) DEFAULT 'open' CHECK (status IN ('open', 'in_discussion', 'closed')),
    source_type VARCHAR(20) DEFAULT 'manual' CHECK (source_type IN ('manual', 'ekap_scraper', 'private_procurement')),
    embedding VECTOR(1536), -- Alıcı Talebinin Vektörel Karşılığı
    deadline TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_requests_hnsw_embedding 
ON reverse_auction_requests USING hnsw (embedding vector_cosine_ops) 
WITH (m = 16, ef_construction = 64);

-- ====================================================================
-- 5. CANLI İHALE TEKLİFLERİ
-- ====================================================================
CREATE TABLE IF NOT EXISTS auction_bids (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    request_id UUID NOT NULL REFERENCES reverse_auction_requests(id) ON DELETE CASCADE,
    bidder_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    patent_id UUID REFERENCES patents(id) ON DELETE SET NULL,
    bid_amount_try NUMERIC(15, 2) NOT NULL,
    proposal_note TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- ====================================================================
-- 6. ESCROW (GÜVENLİ HAVUZ) VE VERİ ODASI TABLOLARI
-- ====================================================================
CREATE TABLE IF NOT EXISTS escrow_transactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patent_id UUID REFERENCES patents(id) ON DELETE SET NULL,
    request_id UUID REFERENCES reverse_auction_requests(id) ON DELETE SET NULL,
    buyer_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    seller_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    total_amount_try NUMERIC(15, 2) NOT NULL CHECK (total_amount_try > 0),
    commission_rate NUMERIC(5, 4) DEFAULT 0.0500,
    platform_fee_try NUMERIC(15, 2) NOT NULL,
    seller_payout_try NUMERIC(15, 2) NOT NULL,
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'in_escrow', 'released', 'refunded', 'disputed')),
    payment_provider VARCHAR(50) DEFAULT 'iyzico_marketplace',
    payment_transaction_id VARCHAR(100),
    notary_approval_document_no VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    escrow_locked_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    refunded_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS digital_ndas (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    patent_id UUID NOT NULL REFERENCES patents(id) ON DELETE CASCADE,
    ip_address VARCHAR(45) NOT NULL,
    user_agent TEXT,
    accepted_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT unique_user_patent_nda UNIQUE (user_id, patent_id)
);

CREATE TABLE IF NOT EXISTS data_room_documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patent_id UUID NOT NULL REFERENCES patents(id) ON DELETE CASCADE,
    document_title VARCHAR(255) NOT NULL,
    s3_bucket VARCHAR(255) NOT NULL,
    s3_key VARCHAR(500) NOT NULL,
    file_size_bytes BIGINT,
    security_level VARCHAR(20) DEFAULT 'restricted' CHECK (security_level IN ('public', 'restricted', 'confidential')),
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

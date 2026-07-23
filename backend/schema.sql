-- ====================================================================
-- PATENT PAZARI - POSTGRESQL VERİTABANI ŞEMASI (IPC & TERSİNE İHALE)
-- Hedef Sektör: İnşaat & Sabit Yapılar (IPC Bölüm E - Section E)
-- ====================================================================

-- 1. GEREKLİ EKLENTİLER (EXTENSIONS)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "ltree";       -- Hiyerarşik ağaç sorguları için (Section.Class.Subclass.Group)
CREATE EXTENSION IF NOT EXISTS "pg_trgm";      -- Metin araması ve trigram indeksleme için

-- ====================================================================
-- 2. HİYERARŞİK IPC KATEGORİLERİ TABLOSU (WIPO IPC STANDARDI)
-- ====================================================================
CREATE TABLE IF NOT EXISTS ipc_categories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code VARCHAR(15) UNIQUE NOT NULL,          -- Örn: 'E', 'E04', 'E04B', 'E04B 2/00', 'E04B 2/02'
    level VARCHAR(20) NOT NULL CHECK (level IN ('section', 'class', 'subclass', 'main_group', 'subgroup')),
    title_tr TEXT NOT NULL,                     -- Türkçe Başlık (Örn: 'Binalar; Duvarlar, Çatılar, Tavanlar')
    title_en TEXT,                              -- İngilizce Başlık (Örn: 'General building structures')
    parent_code VARCHAR(15) REFERENCES ipc_categories(code) ON DELETE CASCADE,
    path LTREE NOT NULL,                        -- Örn: 'E.E04.E04B.E04B_2_00'
    is_construction_sector BOOLEAN DEFAULT FALSE, -- 'E' bölümü için hızlı filtresi (İnşaat Sektörü)
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- Hiyerarşi İndeksleri
CREATE INDEX IF NOT EXISTS idx_ipc_path ON ipc_categories USING GIST (path);
CREATE INDEX IF NOT EXISTS idx_ipc_code ON ipc_categories(code);
CREATE INDEX IF NOT EXISTS idx_ipc_parent ON ipc_categories(parent_code);
CREATE INDEX IF NOT EXISTS idx_ipc_construction ON ipc_categories(is_construction_sector) WHERE is_construction_sector = TRUE;

-- ====================================================================
-- 3. KULLANICILAR & FİRMALAR TABLOSU
-- ====================================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('inventor', 'company', 'admin')), -- Buluşçu (Satıcı) veya Kurum (Alıcı)
    company_name VARCHAR(255),
    tax_number VARCHAR(50),
    is_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- ====================================================================
-- 4. BULUŞLAR / PATENTLER TABLOSU (SATICI TARAF)
-- ====================================================================
CREATE TABLE IF NOT EXISTS patents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    patent_number VARCHAR(50) UNIQUE NOT NULL,  -- Örn: 'TR 2024/01482 B', 'EP3819443'
    title VARCHAR(500) NOT NULL,
    abstract TEXT NOT NULL,
    turkpatent_status VARCHAR(50) DEFAULT 'dogrulandi', -- 'dogrulandi', 'bekliyor'
    listing_type VARCHAR(20) NOT NULL CHECK (listing_type IN ('satis', 'lisans', 'ortaklik')),
    min_expectation_try NUMERIC(15, 2),        -- Asgari Beklenti Bedeli (TL)
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_patents_owner ON patents(owner_id);
CREATE INDEX IF NOT EXISTS idx_patents_number ON patents(patent_number);
CREATE INDEX IF NOT EXISTS idx_patents_title_trgm ON patents USING GIN (title gin_trgm_ops);

-- ====================================================================
-- 5. TERSİNE İHALE TEKNOLOJİ TALEPLERİ TABLOSU (ALICI / ŞİRKET TARAF)
-- ====================================================================
CREATE TABLE IF NOT EXISTS reverse_auction_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(500) NOT NULL,
    problem_statement TEXT NOT NULL,           -- Çözülmek istenen teknik / mühendislik problemi
    target_specifications TEXT,                 -- Beklenen performans / malzeme / maliyet şartnamesi
    preferred_deal_type VARCHAR(20) CHECK (preferred_deal_type IN ('satis', 'lisans', 'ortaklik', 'hepsi')),
    max_budget_try NUMERIC(15, 2),             -- Ayrılan azami bütçe (TL)
    status VARCHAR(20) DEFAULT 'open' CHECK (status IN ('open', 'in_discussion', 'closed')),
    deadline TIMESTAMPTZ,                       -- İhale son teklif tarihi
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_requests_company ON reverse_auction_requests(company_id);
CREATE INDEX IF NOT EXISTS idx_requests_status ON reverse_auction_requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_problem_trgm ON reverse_auction_requests USING GIN (problem_statement gin_trgm_ops);

-- ====================================================================
-- 6. ÇOKTAN ÇOĞA (MANY-TO-MANY) İLİŞKİ ARA TABLOLARI
-- ====================================================================

-- Patent <-> IPC İlişkisi
CREATE TABLE IF NOT EXISTS patent_ipc_categories (
    patent_id UUID NOT NULL REFERENCES patents(id) ON DELETE CASCADE,
    ipc_code VARCHAR(15) NOT NULL REFERENCES ipc_categories(code) ON DELETE CASCADE,
    is_primary BOOLEAN DEFAULT FALSE,           -- Birincil (Primary) IPC sınıfı mı?
    PRIMARY KEY (patent_id, ipc_code)
);

CREATE INDEX IF NOT EXISTS idx_patent_ipc_code ON patent_ipc_categories(ipc_code);

-- Tersine İhale Talebi <-> IPC İlişkisi
CREATE TABLE IF NOT EXISTS request_ipc_categories (
    request_id UUID NOT NULL REFERENCES reverse_auction_requests(id) ON DELETE CASCADE,
    ipc_code VARCHAR(15) NOT NULL REFERENCES ipc_categories(code) ON DELETE CASCADE,
    PRIMARY KEY (request_id, ipc_code)
);

CREATE INDEX IF NOT EXISTS idx_request_ipc_code ON request_ipc_categories(ipc_code);

-- ====================================================================
-- 7. ÖRNEK İNŞAAT SEKTÖRÜ (SECTION E) IPC HİYERARŞİSİ DOLDURMA
-- ====================================================================
INSERT INTO ipc_categories (code, level, title_tr, title_en, parent_code, path, is_construction_sector) VALUES
('E', 'section', 'SABİT YAPILAR (İNŞAAT, MADENCİLİK)', 'FIXED CONSTRUCTIONS', NULL, 'E', TRUE),
('E01', 'class', 'Yol, Demiryolu ve Köprü İnşaatı', 'Construction of roads, railways, or bridges', 'E', 'E.E01', TRUE),
('E02', 'class', 'Su Mühendisliği; Temeller; Toprak Kazısı', 'Hydraulic engineering; Foundations; Soil-shifting', 'E', 'E.E02', TRUE),
('E04', 'class', 'Binalar (Bina İnşaatı, Yapı Elemanları)', 'Building', 'E', 'E.E04', TRUE),
('E04B', 'subclass', 'Genel Yapı Elemanları; Binaların Isı/Ses Yalıtımı ve Korunması', 'General building structures; Walls, roofs, insulation', 'E04', 'E.E04.E04B', TRUE),
('E04C', 'subclass', 'Bina Yapı Malzemeleri; Betornarme, Çelik ve Polimer Elemanlar', 'Structural elements; Building materials', 'E04', 'E.E04.E04C', TRUE),
('E04F', 'subclass', 'Bina Tamamlama İşleri; Kaplamalar, Yaldızlar, Zemin Döşemeleri', 'Finishing work on buildings', 'E04', 'E.E04.E04F', TRUE),
('E21', 'class', 'Yer altı Kazıları; Tüneller; Sondaj', 'Earth or rock drilling; Mining', 'E', 'E.E21', TRUE)
ON CONFLICT (code) DO NOTHING;

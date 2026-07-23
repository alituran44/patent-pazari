# Patent Pazarı — Sınai Mülkiyet & Tersine İhale Platformu

Türkiye'deki patent, faydalı model ve buluş sahiplerini sanayi kuruluşları ve alıcı/yatırımcılarla buluşturan, WIPO IPC Uluslararası Patent Sınıflandırması ve SMK 6769 mevzuatına tam uyumlu tersine ihale (reverse-auction) pazaryeri platformu.

## 🚀 Proje Yapısı

- **Frontend (`index.html`):** Standalone React 18, Babel Standalone, Tailwind CSS, Google Fonts, SVG İkon Sistemi ve LocalStorage persistence ile Vercel üzerinde tek tıkla canlıya alınabilir ön yüz.
- **Backend (`/backend`):** AWS EC2 üzerinde çalışmak üzere tasarlanmış Python & PostgreSQL mimarisi.
  - `schema.sql`: PostgreSQL `ltree` eklentili WIPO IPC hiyerarşi veritabanı şeması ve indeksleri.
  - `epo_service.py`: Avrupa Patent Ofisi (EPO OPS API v3.2) asenkron OAuth2 ve rate limiting uyumlu otomatik IPC kod çekme servisi.
  - `main.py`: FastAPI ile yazılmış, İnşaat Sektörü (IPC Bölüm E - Sabit Yapılar) odaklı tersine ihale arama motoru REST API'si.

---

## 🏛️ Kategori & Hukuki Mimari

1. **Katman 1 — Sınai Mülkiyet Hakkı Türü (SMK 6769):** Patent (20 Yıl), Faydalı Model (10 Yıl), Endüstriyel Tasarım (Locarno), Marka (Nice 1-45), Coğrafi İşaretler, Geleneksel Ürün Adı, Entegre Devre Topografyası.
2. **Katman 2 — Teknik Sektör (IPC):** WIPO 8 Ana Bölüm (A, B, C, D, E, F, G, H) ve 2. Seviye Alt Sınıfları. İlk odak alanı **E — Sabit Yapılar / İnşaat Sektörü**.
3. **Hukuki Yönlendirme:** Görüşme "Olgunlaştı" aşamasında SMK 6769 gereği Noter devir zorunluluğu uyarısı ve TÜRKPATENT vekil rehberi yönlendirmesi.
4. **Moderasyon Kuralı:** Silahlar, Mühimmat (IPC F41, F42) ve Nükleer Teknoloji (IPC G21) kapsam dışı bırakılmıştır.

---

## 🛠️ Kurulum ve Çalıştırma

### Frontend Önizleme
`index.html` dosyasını çift tıklayarak veya bir HTTP sunucusu ile açabilirsiniz:
```bash
python -m http.server 8080
```

### Backend (Python & PostgreSQL)
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

---

## 🔒 Lisans & Sorumluluk Reddi
Platform eşleştirme ve keşfedilebilirlik alanıdır; hukuki danışmanlık veya noterlik sunmaz.

# Kantor Agent Affiliate Online (Dual Threads + X)

Kantor otomatis berbasis **GitHub Actions + Python + Gemini 4 Hydrogen (dengan degradasi otomatis ke model gratis tertinggi) + Google Sheets** untuk niche **Gadget Meja Kerja & Perlengkapan Kos/Rumah Tangga**.

## Susunan Agent
1. **Agent 1 (`src/shopee_hunter.py`) — Product Hunter:** Membaca antrean produk `PENDING` dari Google Sheets (`Antrean_Produk`). Jika antrean kosong dan `SHOPEE_APP_ID` + `SHOPEE_SECRET` terisi, otomatis mencari produk **Rating >= 4.7** & **Komisi XTRA** lewat Shopee Affiliate Open API.
2. **Agent 2 (`src/copywriter_agent.py`) — Copywriter & QC:** Menggunakan **Gemini 4 Hydrogen** (`google-genai`) dengan rantai fallback bertahap (`gemini-4-hydrogen` → `gemini-3.8-flash` → `gemini-3.6-flash` → `gemini-3.5-flash` → `gemini-3.5-flash-lite` → `gemini-2.5-pro` → `gemini-2.5-flash-lite` → dynamic discovery) untuk membuat 2 gaya tulisan (gaya ngobrol Threads dan gaya kurasi X), lalu disaring oleh modul QC agar **tidak ada link bocor** di utas utama Threads.
3. **Agent 3 (`src/publishers.py`) — Publisher & Dispatcher:**
   - Auto-post ke **Meta Threads** (utas utama + foto jika ada, lalu *auto-reply* link Shopee di bawahnya).
   - Mengirim draf **X** siap *copy-paste* ke **Telegram** Anda.
4. **Agent 4 & 5 (`src/engagement_manager.py`) — Admin Komentar & Office Manager:** Mengecek komentar baru di Threads setiap jam **21.00 WIB**, membuat saran balasan ramah dengan Gemini, mencatat ke tab `Komentar_Threads`, mengecek umur token Threads, dan mengingatkan jika stok produk `PENDING` menipis.

## Struktur Google Sheets (`Kantor Affiliate - Antrean & Laporan`)
- **`Dashboard_&_Panduan`**: Ringkasan real-time + legenda warna.
- **`Antrean_Produk`**:
  - **Kuning (`#FFF2CC`)**: Input Wajib User (`Status`, `Kategori`, `Nama_Produk`, `Masalah_dan_Keunggulan`, `Link_Affiliate`).
  - **Biru Muda (`#D9E8FB`)**: Input Opsional User (`Harga_Kisaran`, `URL_Foto_Publik`, `Catatan_User_untuk_AI`).
  - **Abu-abu (`#F3F3F3`)**: Diisi otomatis oleh Agent (`ID`, `Sumber`, `Waktu_Tayang_WIB`, `Threads_Post_ID`, `Link_Post_Threads`, `Model_AI_Dipakai`, `Catatan_Agent`).
- **`Log_Performa`**: Riwayat seluruh naskah, status publikasi, dan model AI yang dipakai.
- **`Komentar_Threads`**: Daftar komentar masuk + saran balasan AI + opsi edit manual.
- **`Pengaturan_Agent`**: Persona AI, model utama, rantai fallback Gemini, kata kunci Auto Hunter, dan batas stok minimum.

## Pengujian Lokal
```bash
pip install -r requirements-dev.txt
pytest -v
```

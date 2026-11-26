# Kimiabill Foundation — Kantor Agent Affiliate (Anti-Bot & Humanized Threads)

Sistem automasi konten Meta Threads, kurasi X, dan laporan operasional via Telegram + Google Sheets yang dirancang agar akun tampil natural seperti pengguna manusia asli (anti-bot & anti-spam).

## Arsitektur Inti (Agent 1–4)

1. **Agent 1 — Hunter & Rasio Konten ([`src/shopee_hunter.py`](src/shopee_hunter.py))**
   - Membaca antrean `Antrean_Produk` dan riwayat `Log_Performa`.
   - Menegakkan **rasio pengaman 60–70% Organik : 30–40% Soft Affiliate**:
     - Maksimal **1 post `SOFT_AFFILIATE` per hari (WIB)**.
     - Minimal **2 post `ORGANIK_*` berturut-turut** sebelum slot `SOFT_AFFILIATE` berikutnya dibuka.
   - Jika giliran organik tiba dan antrean `ORGANIK_*` kosong, agent otomatis membuat topik organik sesuai niche (`Gadget Meja Kerja & Perlengkapan Kos/Rumah Tangga`).
   - Memvalidasi foto produk (`URL_Foto_Publik` HTTPS wajib HTTP 200) untuk `SOFT_AFFILIATE`. Jika kosong atau gagal diakses, [`photo_finder.py`](src/photo_finder.py) otomatis mencari URL foto `HTTP 200 image/*` (via `SERPER_API_KEY`, `GOOGLE_CSE_API_KEY`+`GOOGLE_CSE_CX`, atau Gemini Grounding) dan menyimpannya ke Google Sheets; jika tetap gagal, baris ditandai `NEED_PHOTO` + alert Telegram.

2. **Agent 2 — Copywriter & Strict Anti-Bot QC ([`src/copywriter_agent.py`](src/copywriter_agent.py))**
   - **Context-Aware Anti-Repetition**: membaca hingga **10 naskah terakhir** di `Log_Performa` dan melarang pengulangan kalimat pembuka (*hook*) yang serupa.
   - **Rotasi Angle Dinamis**: *Honest Micro-Review* (wajib menyebut 1 kekurangan minor/trade-off realistis), *Problem-Specific Story*, *Comparison*, dan *Casual Thought / Question*.
   - **QC Validator Ketat (maks 3x regenerasi otomatis)**: menolak frasa klise AI/affiliate (`"sering kesel gak sih"`, `"pernah gak sih"`, `"jujur seneng banget"`, `"tanpa bikin kantong jebol"`, `"bikin mundur teratur"`, `"spill link"`, `"auto rapi"`, `"wajib punya"`, `"solusi terbaik"`, `"langsung check out"`, dll.), penomoran `1/2` / `2/2`, hashtag, bullet list kaku, lebih dari 1 emoji, URL bocor di utas utama, dan kapitalisasi tidak konsisten.

3. **Agent 3 — Visual Gatekeeper, Publisher & Telegram ([`src/publishers.py`](src/publishers.py))**
   - Konten `ORGANIK_*` diterbitkan sebagai **1 utas tunggal tanpa balasan link** dan tanpa penomoran `1/2` atau `2/2`.
   - Konten `SOFT_AFFILIATE` wajib lolos verifikasi foto HTTPS `HTTP 200`, tidak selalu menyebut ajakan link di utas utama, dan menunggu **jeda acak 90–300 detik** sebelum mengirim balasan link dengan kalimat natural yang bervariasi.

4. **Agent 4 — Engagement Manager ([`src/engagement_manager.py`](src/engagement_manager.py))**
   - Mengambil komentar baru pada postingan `PUBLISHED` dan membuat saran balasan santai 1–2 kalimat ke tab `Komentar_Threads` & Telegram.
   - Dilarang menyisipkan link atau jualan jika komentar audiens hanya menanggapi topik atau bercanda.

*(Juga dilengkapi workflow pendukung riset tren & Super Audit mingguan di [`src/trend_researcher.py`](src/trend_researcher.py) dan [`src/ceo_auditor.py`](src/ceo_auditor.py)).*

---

## Konfigurasi Kolom Google Sheets

### Tab `Antrean_Produk`
- `ID`, `Status` (`PENDING`, `NEED_PHOTO`, `PUBLISHED`, `DRAFT_ONLY`, `ERROR`, `CONTOH`, `SKIP`)
- `Kategori`, `Nama_Produk`, `Harga_Kisaran`, `Masalah_dan_Keunggulan`, `Link_Affiliate`, `URL_Foto_Publik`
- `Catatan_User_untuk_AI`, `Sumber`, `Waktu_Tayang_WIB`, `Threads_Post_ID`, `Link_Post_Threads`, `Model_AI_Dipakai`, `Catatan_Agent`
- **`Tipe_Konten`**:
  - `ORGANIK_OPINI`: opini kasual/cerita relatable (tanpa link & tanpa foto wajib)
  - `ORGANIK_TIPS`: tips singkat praktis dalam paragraf percakapan (tanpa link)
  - `ORGANIK_DISKUSI`: pertanyaan ringan pemancing komentar (tanpa link)
  - `SOFT_AFFILIATE`: ulasan produk dengan link affiliate (**wajib `URL_Foto_Publik` HTTP 200**)

### Tab `Log_Performa`
Mencatat riwayat tayang beserta kolom **`Tipe_Konten`**, `Draft_Threads`, `Status_Threads`, `Model_AI_Dipakai`, dan `Catatan_QC` untuk kontrol rasio dan anti-repetisi 10 postingan terakhir.

---

## Jadwal GitHub Actions (Humanized)
- [`publish_content.yml`](.github/workflows/publish_content.yml): 3x sehari pada jam aktif WIB (`12:00`, `17:00`, `20:00` WIB) dengan **random jitter delay 3–25 menit** sebelum publikasi pada eksekusi terjadwal.
- [`engagement_manager.yml`](.github/workflows/engagement_manager.yml): harian pukul `21:00` WIB.

## Model AI & Fallback ([`src/config.py`](src/config.py))
- Primary & Fallback Gemini aktif: `gemini-4-hydrogen` -> `gemini-3.8-flash` -> `gemini-3.7-flash` -> `gemini-3.6-flash` -> `gemini-3.5-flash` -> `gemini-3.5-flash-lite`
- Cadangan OpenRouter aktif: `google/gemma-4-31b-it:free`, `google/gemma-4-26b-a4b-it:free`, `nvidia/nemotron-3-ultra-550b-a55b:free`, `google/gemini-3.8-flash`, `google/gemini-3.5-flash-lite`, `openrouter/free`

## Pengujian Lokal
```bash
.venv/bin/pytest -v
```

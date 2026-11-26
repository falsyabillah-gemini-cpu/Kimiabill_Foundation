"""Agent 1 — Product Hunter.

Membaca antrean `PENDING` dari Google Sheets (`Antrean_Produk`).
Jika antrean kosong dan `SHOPEE_APP_ID` + `SHOPEE_SECRET` terisi, otomatis
mencari produk Rating >= 4.7 & Komisi XTRA melalui Shopee Affiliate Open API.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from typing import Any

import requests

from src.config import (
    AFFILIATE_CONTENT_TYPE,
    ORGANIC_CONTENT_TYPES,
    Settings,
    is_organic_content_type,
    normalize_content_type,
    now_wib_str,
    sheet_url,
    today_wib_str,
)
from src.publishers import (
    format_need_photo_telegram_message,
    send_telegram_message,
    verify_public_image_url,
)
from src.sheets_client import ProductRow, SheetsClient

SHOPEE_GRAPHQL_URL = "https://open-api.affiliate.shopee.co.id/graphql"

DEFAULT_ORGANIC_IDEAS: tuple[dict[str, str], ...] = (
    {
        "tipe_konten": "ORGANIK_OPINI",
        "kategori": "Meja Kerja",
        "nama_produk": "Kebiasaan kecil bikin meja kerja gak sumpek",
        "masalah_dan_keunggulan": (
            "Opini jujur soal setup meja kerja: barang mahal gak selalu bikin betah kalau kabel berantakan "
            "dan meja penuh barang jarang kepake. Lebih penting tata letak lega dan pencahayaan nyaman."
        ),
    },
    {
        "tipe_konten": "ORGANIK_TIPS",
        "kategori": "Perlengkapan Kos",
        "nama_produk": "Trik beres-beres kamar kos sempit dalam 10 menit",
        "masalah_dan_keunggulan": (
            "Tips praktis anak kos: pakai prinsip satu tempat satu fungsi, simpan barang musiman di kotak tertutup "
            "di bawah kasur/meja, dan pisahkan area tidur sama area kerja biar pikiran gak capek."
        ),
    },
    {
        "tipe_konten": "ORGANIK_DISKUSI",
        "kategori": "Meja Kerja",
        "nama_produk": "Diskusi setup kerja tim lampu monitor vs lampu meja biasa",
        "masalah_dan_keunggulan": (
            "Pertanyaan santai buat yang kerja malam di depan laptop: lebih nyaman pakai cahaya ruang terang "
            "atau lampu sorot fokus ke meja aja biar mata gak cepet pegal?"
        ),
    },
    {
        "tipe_konten": "ORGANIK_OPINI",
        "kategori": "Perlengkapan Kos",
        "nama_produk": "Barang kosan yang keliatan sepele tapi ngaruh banget tiap pagi",
        "masalah_dan_keunggulan": (
            "Refleksi ringan anak kos soal printilan harian: kadang yang bikin pagi gak buru-buru bukan alarm keras, "
            "tapi naruh kunci, charger, dan botol minum di satu titik tetap sebelum tidur."
        ),
    },
    {
        "tipe_konten": "ORGANIK_TIPS",
        "kategori": "Meja Kerja",
        "nama_produk": "Aturan 3 barang di permukaan meja biar fokus kerja gak pecah",
        "masalah_dan_keunggulan": (
            "Tips sederhana biar meja gak jadi gudang: sisakan maksimal laptop, botol minum, dan catatan kecil di atas meja; "
            "sisanya masuk laci atau organizer samping."
        ),
    },
    {
        "tipe_konten": "ORGANIK_DISKUSI",
        "kategori": "Perlengkapan Kos",
        "nama_produk": "Kebiasaan masak simpel di kosan vs jajan terus",
        "masalah_dan_keunggulan": (
            "Curhat ringan soal hidup ngekos: tim nyiapin alat dapur minimalis sendiri buat sarapan/kopi "
            "atau full tim beli di luar biar gak ribet nyuci?"
        ),
    },
)


def _is_valid_affiliate_link(link: str) -> bool:
    low = (link or "").strip().lower()
    if not low or "contoh_ganti_link" in low:
        return False
    return low.startswith("http://") or low.startswith("https://")


def _build_shopee_auth_header(app_id: str, secret: str, payload_str: str, timestamp: int) -> str:
    raw = f"{app_id}{timestamp}{payload_str}{secret}"
    signature = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"SHA256 Credential={app_id}, Timestamp={timestamp}, Signature={signature}"


def _format_rupiah(value: Any) -> str:
    try:
        amount = int(float(str(value).replace(",", "").strip()))
        return f"Rp{amount:,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(value or "")


def fetch_shopee_xtra_product(
    *,
    app_id: str,
    secret: str,
    keywords: list[str],
    min_rating: float = 4.7,
    existing_names: set[str] | None = None,
    http_post: Any = requests.post,
) -> dict[str, str] | None:
    """Cari 1 produk Rating >= min_rating & Komisi XTRA via Shopee Affiliate GraphQL API."""
    if not app_id or not secret or not keywords:
        return None

    existing_low = {n.strip().lower() for n in (existing_names or set()) if n}
    shuffled = list(keywords)
    random.shuffle(shuffled)

    query = """
    query ProductOffer($keyword: String!, $page: Int, $limit: Int) {
      productOfferV2(keyword: $keyword, listType: 0, sortType: 2, page: $page, limit: $limit) {
        nodes {
          itemId
          productName
          priceMin
          priceMax
          ratingStar
          sales
          commissionRate
          sellerCommissionRate
          offerLink
          productLink
          imageUrl
        }
      }
    }
    """

    for kw in shuffled[:4]:
        payload = {
            "query": query,
            "variables": {"keyword": kw, "page": 1, "limit": 20},
        }
        payload_str = json.dumps(payload, separators=(",", ":"))
        ts = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "Authorization": _build_shopee_auth_header(app_id, secret, payload_str, ts),
        }
        try:
            resp = http_post(SHOPEE_GRAPHQL_URL, data=payload_str, headers=headers, timeout=25)
            if resp.status_code != 200:
                print(f"[hunter] Shopee API HTTP {resp.status_code} untuk keyword '{kw}'")
                continue
            body = resp.json()
            nodes = (
                body.get("data", {})
                .get("productOfferV2", {})
                .get("nodes", [])
            )
            for node in nodes:
                name = str(node.get("productName", "")).strip()
                if not name or name.lower() in existing_low:
                    continue
                try:
                    rating = float(node.get("ratingStar") or 0)
                except (TypeError, ValueError):
                    rating = 0.0
                try:
                    seller_comm = float(node.get("sellerCommissionRate") or 0)
                    base_comm = float(node.get("commissionRate") or 0)
                except (TypeError, ValueError):
                    seller_comm, base_comm = 0.0, 0.0

                if rating < min_rating:
                    continue
                # Prioritaskan produk Komisi XTRA (sellerCommissionRate > 0)
                if seller_comm <= 0 and base_comm <= 0:
                    continue

                link = str(node.get("offerLink") or node.get("productLink") or "").strip()
                if not _is_valid_affiliate_link(link):
                    continue

                price_str = _format_rupiah(node.get("priceMin"))
                comm_pct = round((seller_comm + base_comm) * 100, 1)
                return {
                    "kategori": kw.title(),
                    "nama_produk": name,
                    "harga_kisaran": price_str,
                    "masalah_dan_keunggulan": (
                        f"Produk pilihan '{kw}' rating {rating:.1f}⭐ "
                        f"(terjual {node.get('sales', 'banyak')}, komisi ~{comm_pct}%). "
                        "Bantu kebutuhan harian lebih rapi, praktis, dan hemat."
                    ),
                    "link_affiliate": link,
                    "url_foto_publik": str(node.get("imageUrl") or "").strip(),
                }
        except Exception as exc:
            print(f"[hunter] Gagal query Shopee API untuk '{kw}': {exc}")
            continue

    return None


def determine_content_slot(
    logs: list[dict[str, str]],
    *,
    today_date: str | None = None,
) -> tuple[bool, str]:
    """Tentukan apakah giliran saat ini boleh `SOFT_AFFILIATE` atau wajib `ORGANIK_*`.

    Aturan rasio (60-70% organik : 30-40% affiliate):
    - Maksimal 1 post `SOFT_AFFILIATE` per hari (WIB).
    - Minimal 2 post `ORGANIK_*` di antara post `SOFT_AFFILIATE`.
    """
    today = today_date or today_wib_str()
    published_logs = [
        entry
        for entry in logs
        if entry.get("Status_Threads", "").strip().upper() == "PUBLISHED"
    ]

    for entry in published_logs:
        tanggal = entry.get("Tanggal_WIB", "").strip()
        tipe = normalize_content_type(entry.get("Tipe_Konten", ""))
        if tanggal.startswith(today) and tipe == AFFILIATE_CONTENT_TYPE:
            return (
                False,
                f"Kuota SOFT_AFFILIATE hari ini ({today}) sudah terpakai 1x; giliran konten ORGANIK.",
            )

    consecutive_organic = 0
    for entry in published_logs:
        tipe = normalize_content_type(entry.get("Tipe_Konten", ""))
        if tipe == AFFILIATE_CONTENT_TYPE:
            break
        consecutive_organic += 1

    if consecutive_organic < 2:
        return (
            False,
            f"Rasio pengaman aktif: baru {consecutive_organic}/2 post ORGANIK sebelum jatah SOFT_AFFILIATE.",
        )

    return (
        True,
        f"Slot SOFT_AFFILIATE terbuka ({consecutive_organic} post ORGANIK terpenuhi & kuota harian {today} tersedia).",
    )


def build_auto_organic_row(
    sheet_config: dict[str, str],
    *,
    recent_logs: list[dict[str, str]] | None = None,
) -> ProductRow:
    """Bangun ide konten organik otomatis berdasarkan niche saat antrean organik kosong."""
    logs = recent_logs or []
    recent_topics = {
        entry.get("Nama_Produk", "").strip().lower()
        for entry in logs[:10]
        if entry.get("Nama_Produk")
    }
    last_organic_type = ""
    for entry in logs:
        t = normalize_content_type(entry.get("Tipe_Konten", ""))
        if is_organic_content_type(t):
            last_organic_type = t
            break

    candidates = [
        idea
        for idea in DEFAULT_ORGANIC_IDEAS
        if idea["nama_produk"].strip().lower() not in recent_topics
    ] or list(DEFAULT_ORGANIC_IDEAS)

    rotated = [idea for idea in candidates if idea["tipe_konten"] != last_organic_type]
    chosen = (rotated or candidates)[0]
    niche = sheet_config.get(
        "Niche_Utama", "Gadget Meja Kerja & Perlengkapan Kos/Rumah Tangga"
    )

    return ProductRow(
        row_number=0,
        product_id="ORG-AUTO",
        status="PENDING",
        kategori=chosen["kategori"],
        nama_produk=chosen["nama_produk"],
        harga_kisaran="",
        masalah_dan_keunggulan=chosen["masalah_dan_keunggulan"],
        link_affiliate="",
        url_foto_publik="",
        catatan_user_untuk_ai=f"Auto-topik organik sesuai niche: {niche}. DILARANG sisipkan link/jualan.",
        sumber="ORGANIC_AUTO_IDEA",
        tipe_konten=chosen["tipe_konten"],
    )


def pick_next_product(
    sheets: SheetsClient,
    settings: Settings,
    sheet_config: dict[str, str],
    *,
    http_post: Any = requests.post,
    http_get: Any = requests.get,
    today_date: str | None = None,
) -> ProductRow | None:
    """Pilih item tayang berikutnya sesuai rasio Organik vs Soft Affiliate + validasi foto."""
    all_rows = sheets.list_queue_rows()
    logs = sheets.list_performance_logs(limit=40)
    needs_affiliate, ratio_reason = determine_content_slot(logs, today_date=today_date)
    print(f"[hunter] Keputusan rasio: {ratio_reason}")

    if not needs_affiliate:
        for row in all_rows:
            if row.status != "PENDING" or not is_organic_content_type(row.tipe_konten):
                continue
            if not row.nama_produk and not row.masalah_dan_keunggulan:
                sheets.update_queue_row_fields(
                    row.row_number,
                    {
                        "Status": "ERROR",
                        "Catatan_Agent": f"[{now_wib_str()}] Ide organik kosong (isi Nama_Produk/Masalah_dan_Keunggulan).",
                    },
                )
                continue
            if not settings.dry_run:
                sheets.update_queue_row_fields(
                    row.row_number,
                    {
                        "Status": "PROCESSING",
                        "Sumber": row.sumber or "SHEET_MANUAL",
                    },
                )
            return row

        auto_organic = build_auto_organic_row(sheet_config, recent_logs=logs)
        print(
            f"[hunter] Antrean ORGANIK kosong -> auto-topik {auto_organic.tipe_konten}: {auto_organic.nama_produk}"
        )
        return auto_organic

    # Giliran SOFT_AFFILIATE
    pending_affiliate_rows = [
        r
        for r in all_rows
        if r.status == "PENDING"
        and normalize_content_type(r.tipe_konten) == AFFILIATE_CONTENT_TYPE
    ]
    need_photo_marked = 0
    gs_link = sheet_url(settings.google_sheet_id) if settings.google_sheet_id else ""

    for idx, row in enumerate(pending_affiliate_rows):
        missing: list[str] = []
        if not row.nama_produk:
            missing.append("Nama_Produk")
        if not row.masalah_dan_keunggulan:
            missing.append("Masalah_dan_Keunggulan")
        if not _is_valid_affiliate_link(row.link_affiliate):
            missing.append("Link_Affiliate valid")

        if missing:
            sheets.update_queue_row_fields(
                row.row_number,
                {
                    "Status": "ERROR",
                    "Catatan_Agent": (
                        f"[{now_wib_str()}] Input wajib belum lengkap/valid: {', '.join(missing)}"
                    ),
                },
            )
            print(f"[hunter] Baris {row.row_number} ditandai ERROR karena kurang: {missing}")
            continue

        photo_ok, photo_reason = verify_public_image_url(
            row.url_foto_publik, http_get=http_get
        )
        if not photo_ok:
            sheets.update_queue_row_fields(
                row.row_number,
                {
                    "Status": "NEED_PHOTO",
                    "Catatan_Agent": (
                        f"[{now_wib_str()}] Gatekeeping foto ditolak: {photo_reason}"
                    ),
                },
            )
            need_photo_marked += 1
            print(
                f"[hunter] Baris {row.row_number} ({row.nama_produk}) -> NEED_PHOTO: {photo_reason}"
            )
            if settings.telegram_bot_token and settings.telegram_chat_id:
                send_telegram_message(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                    html_text=format_need_photo_telegram_message(
                        product_name=row.nama_produk,
                        kategori=row.kategori,
                        row_number=row.row_number,
                        reason=photo_reason,
                        sheet_link=gs_link,
                    ),
                    http_post=http_post,
                )
            has_later_photo = any(
                bool(r.url_foto_publik.strip())
                for r in pending_affiliate_rows[idx + 1 :]
            )
            if has_later_photo and need_photo_marked < 3:
                continue
            return None

        if not settings.dry_run:
            sheets.update_queue_row_fields(
                row.row_number,
                {
                    "Status": "PROCESSING",
                    "Sumber": row.sumber or "SHEET_MANUAL",
                },
            )
        return row

    # Antrean PENDING SOFT_AFFILIATE kosong -> coba Shopee Affiliate Auto Hunter jika kredensial ada
    if settings.shopee_app_id and settings.shopee_secret and need_photo_marked == 0:
        raw_kw = sheet_config.get("Kata_Kunci_Auto_Hunter", "")
        keywords = [k.strip() for k in raw_kw.split(",") if k.strip()]
        try:
            min_rating = float(sheet_config.get("Min_Rating_Shopee", "4.7") or 4.7)
        except ValueError:
            min_rating = 4.7

        existing_names = {r.nama_produk for r in all_rows if r.nama_produk}
        found = fetch_shopee_xtra_product(
            app_id=settings.shopee_app_id,
            secret=settings.shopee_secret,
            keywords=keywords,
            min_rating=min_rating,
            existing_names=existing_names,
            http_post=http_post,
        )
        if found:
            photo_ok, _ = verify_public_image_url(
                found.get("url_foto_publik", ""), http_get=http_get
            )
            if photo_ok:
                status_awal = "PENDING" if settings.dry_run else "PROCESSING"
                new_row = sheets.append_queue_product(
                    status=status_awal,
                    kategori=found["kategori"],
                    nama_produk=found["nama_produk"],
                    harga_kisaran=found["harga_kisaran"],
                    masalah_dan_keunggulan=found["masalah_dan_keunggulan"],
                    link_affiliate=found["link_affiliate"],
                    url_foto_publik=found["url_foto_publik"],
                    sumber="SHOPEE_AUTO_HUNTER",
                    catatan_agent=f"[{now_wib_str()}] Ditemukan otomatis oleh Shopee Hunter",
                    tipe_konten=AFFILIATE_CONTENT_TYPE,
                )
                print(f"[hunter] Auto Hunter menambahkan produk baru: {new_row.nama_produk}")
                return new_row

    print("[hunter] Tidak ada item siap tayang pada slot SOFT_AFFILIATE saat ini.")
    return None

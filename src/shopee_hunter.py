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

from src.config import Settings, now_wib_str
from src.sheets_client import ProductRow, SheetsClient

SHOPEE_GRAPHQL_URL = "https://open-api.affiliate.shopee.co.id/graphql"


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


def pick_next_product(
    sheets: SheetsClient,
    settings: Settings,
    sheet_config: dict[str, str],
    *,
    http_post: Any = requests.post,
) -> ProductRow | None:
    """Ambil 1 produk `PENDING` yang valid, atau jalankan Shopee Auto Hunter bila kosong."""
    all_rows = sheets.list_queue_rows()

    for row in all_rows:
        if row.status != "PENDING":
            continue
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

        if not settings.dry_run:
            sheets.update_queue_row_fields(
                row.row_number,
                {
                    "Status": "PROCESSING",
                    "Sumber": row.sumber or "SHEET_MANUAL",
                },
            )
        return row

    # Antrean PENDING kosong -> coba Shopee Affiliate Auto Hunter jika kredensial ada
    if settings.shopee_app_id and settings.shopee_secret:
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
            )
            print(f"[hunter] Auto Hunter menambahkan produk baru: {new_row.nama_produk}")
            return new_row

    print("[hunter] Tidak ada produk PENDING di Antrean_Produk.")
    return None

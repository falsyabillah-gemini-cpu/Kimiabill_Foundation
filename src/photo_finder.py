"""Auto Photo Finder — Pencari URL Foto Publik Produk (`HTTP 200 image/*`).

Digunakan otomatis saat produk `SOFT_AFFILIATE` di `Antrean_Produk` memiliki
`URL_Foto_Publik` kosong/tidak valid, sebelum memicu status `NEED_PHOTO`.

Urutan pencarian otomatis:
1. Serper.dev Google Images API (`SERPER_API_KEY` — 2.500 query gratis).
2. Google Custom Search Image API (`GOOGLE_CSE_API_KEY` + `GOOGLE_CSE_CX`).
3. Gemini + Google Search Grounding (opsional jika Image Search API belum dipasang):
   mencari halaman produk/brand resmi publik & mengekstrak `og:image`.
Setiap kandidat URL wajib lolos `verify_public_image_url` (`HTTPS` + `HTTP 200` + `image/*`).
"""
from __future__ import annotations

import re
from typing import Any

import requests
from pydantic import BaseModel, Field

from src.config import Settings
from src.publishers import verify_public_image_url
from src.sheets_client import ProductRow

SERPER_IMAGES_URL = "https://google.serper.dev/images"
GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"

PREFERRED_CDN_TOKENS: tuple[str, ...] = (
    "susercontent.com",
    "cf.shopee",
    "tokopedia",
    "shopify",
    "static-src",
)


class _ImageSearchLinks(BaseModel):
    candidate_urls: list[str] = Field(
        default_factory=list,
        description="Maks 5 URL halaman produk publik resmi atau URL gambar HTTPS langsung.",
    )


def build_product_image_query(product: ProductRow) -> str:
    """Bangun query pencarian singkat & relevan dari nama produk + toko."""
    raw_name = re.sub(r"[\[\]()【】|]+", " ", product.nama_produk or "")
    words = [w for w in raw_name.split() if len(w) > 1][:8]
    shop_match = re.search(r"Toko:\s*([^|]+)", product.catatan_user_untuk_ai or "")
    shop_hint = shop_match.group(1).strip().split()[0] if shop_match else ""
    parts = words + ([shop_hint] if shop_hint and shop_hint.lower() not in " ".join(words).lower() else [])
    query = " ".join(parts).strip()
    return f"{query} shopee" if query else ""


def _prioritize_image_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        clean = (u or "").strip()
        if clean.startswith("https://") and clean not in seen:
            seen.add(clean)
            unique.append(clean)
    return sorted(
        unique,
        key=lambda u: any(tok in u.lower() for tok in PREFERRED_CDN_TOKENS),
        reverse=True,
    )


def search_via_serper(
    query: str,
    api_key: str,
    *,
    http_post: Any = requests.post,
    http_get: Any = requests.get,
) -> str | None:
    if not api_key or not query:
        return None
    try:
        resp = http_post(
            SERPER_IMAGES_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "gl": "id", "hl": "id", "num": 8},
            timeout=20,
        )
        if getattr(resp, "status_code", None) != 200:
            return None
        images = resp.json().get("images", []) or []
        candidates = _prioritize_image_urls(
            [str(item.get("imageUrl", "")).strip() for item in images]
        )
        for url in candidates[:6]:
            ok, _ = verify_public_image_url(url, http_get=http_get)
            if ok:
                return url
    except Exception:
        return None
    return None


def search_via_google_cse(
    query: str,
    api_key: str,
    cx: str,
    *,
    http_get: Any = requests.get,
) -> str | None:
    if not api_key or not cx or not query:
        return None
    try:
        resp = http_get(
            GOOGLE_CSE_URL,
            params={
                "key": api_key,
                "cx": cx,
                "q": query,
                "searchType": "image",
                "num": 8,
                "gl": "id",
            },
            timeout=20,
        )
        if getattr(resp, "status_code", None) != 200:
            return None
        items = resp.json().get("items", []) or []
        candidates = _prioritize_image_urls(
            [str(item.get("link", "")).strip() for item in items]
        )
        for url in candidates[:6]:
            ok, _ = verify_public_image_url(url, http_get=http_get)
            if ok:
                return url
    except Exception:
        return None
    return None


def _extract_og_image(page_url: str, *, http_get: Any = requests.get) -> str | None:
    if not page_url.startswith("https://"):
        return None
    try:
        resp = http_get(
            page_url,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0 (compatible; KimiabillBot/1.0)"},
        )
        if getattr(resp, "status_code", None) != 200:
            return None
        html_text = getattr(resp, "text", "") or ""
        m = re.search(
            r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            html_text,
            re.IGNORECASE,
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
                html_text,
                re.IGNORECASE,
            )
        if m:
            candidate = m.group(1).strip()
            if candidate.startswith("https://"):
                return candidate
    except Exception:
        return None
    return None


def search_via_gemini_grounding(
    query: str,
    gemini: Any,
    *,
    http_get: Any = requests.get,
) -> str | None:
    if gemini is None or not query:
        return None
    try:
        res, _ = gemini.generate_structured(
            system_prompt=(
                "Kamu pencari link halaman resmi produk / marketplace non-login yang punya meta og:image publik.\n"
                "Berikan maks 5 URL HTTPS yang relevan dengan produk."
            ),
            user_prompt=f"Cari halaman web resmi atau katalog publik untuk produk: {query}",
            response_model=_ImageSearchLinks,
            temperature=0.1,
            use_google_search=True,
        )
        for url in (res.candidate_urls or [])[:5]:
            clean = (url or "").strip()
            if not clean.startswith("https://"):
                continue
            ok, _ = verify_public_image_url(clean, http_get=http_get)
            if ok:
                return clean
            og_img = _extract_og_image(clean, http_get=http_get)
            if og_img:
                ok_og, _ = verify_public_image_url(og_img, http_get=http_get)
                if ok_og:
                    return og_img
    except Exception:
        return None
    return None


def find_public_product_photo(
    product: ProductRow,
    settings: Settings,
    *,
    gemini: Any = None,
    http_post: Any = requests.post,
    http_get: Any = requests.get,
) -> tuple[str, str]:
    """Cari URL gambar produk valid HTTP 200 secara otomatis. Mengembalikan `(url, sumber)`."""
    query = build_product_image_query(product)
    if not query:
        return "", "Query produk kosong"

    if settings.serper_api_key:
        found = search_via_serper(
            query,
            settings.serper_api_key,
            http_post=http_post,
            http_get=http_get,
        )
        if found:
            return found, "serper_image_search"

    if settings.google_cse_api_key and settings.google_cse_cx:
        found = search_via_google_cse(
            query,
            settings.google_cse_api_key,
            settings.google_cse_cx,
            http_get=http_get,
        )
        if found:
            return found, "google_cse_image_search"

    if gemini is not None:
        found = search_via_gemini_grounding(
            query.replace(" shopee", ""),
            gemini,
            http_get=http_get,
        )
        if found:
            return found, "gemini_grounding"

    return "", "Belum ada Image Search API (SERPER_API_KEY / GOOGLE_CSE) atau kandidat HTTP 200 tidak ditemukan"

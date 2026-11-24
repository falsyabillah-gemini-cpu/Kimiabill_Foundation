"""Importer CSV massal dari ekspor Link Produk Shopee Affiliate (`LinkProdukSekaligus*.csv`)."""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from src.config import now_wib_str
from src.sheets_client import SheetsClient

_NOISE_TAGS_RE = re.compile(
    r"^\s*(\(HOT\)|\[SNI\]|-COD\+Stok tersedia-|COD\s+|PROMO\s+|NEW EDITION \d{4}\s*-\s*\[ORI\]\s*\[LIMITED\]\s*)",
    re.IGNORECASE,
)
_BRACKET_TAGS_RE = re.compile(r"\[(?:Pengiriman dari [^\]]+|Garansi [^\]]+|PT)\]", re.IGNORECASE)


def normalize_shopee_price(raw_price: str) -> str:
    """Ubah format harga Shopee CSV ('48,8RB', '1,4JT') menjadi format Rupiah rapi."""
    cleaned = (raw_price or "").strip().upper().replace("RP", "").strip()
    if not cleaned:
        return ""
    try:
        if cleaned.endswith("JT"):
            num = float(cleaned[:-2].replace(".", "").replace(",", "."))
            val = int(round(num * 1_000_000))
            return f"Rp{val:,}".replace(",", ".")
        if cleaned.endswith("RB"):
            num = float(cleaned[:-2].replace(".", "").replace(",", "."))
            val = int(round(num * 1_000))
            return f"Rp{val:,}".replace(",", ".")
        num = float(cleaned.replace(".", "").replace(",", "."))
        return f"Rp{int(round(num)):,}".replace(",", ".")
    except ValueError:
        return raw_price.strip()


def clean_product_title(raw_name: str) -> str:
    """Bersihkan tag promosi berlebih di awal/akhir judul Shopee tanpa membuang info utama."""
    name = (raw_name or "").replace("\u00a0", " ").strip()
    while True:
        updated = _NOISE_TAGS_RE.sub("", name).strip()
        if updated == name:
            break
        name = updated
    name = _BRACKET_TAGS_RE.sub("", name)
    name = re.sub(r"\s*<\s*Garansi [^>]+>", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s{2,}", " ", name).strip(" -|/")
    return name


def classify_category(name: str) -> str:
    """Kelompokkan produk ke kategori niche Meja Kerja & Kos/Rumah Tangga."""
    low = name.lower()
    if any(k in low for k in ("charger", "usb", "speaker", "audio", "soundbar", "stop kontak", "outlet")):
        return "Gadget & Audio"
    if any(k in low for k in ("lampu", "kipas", "fan")):
        return "Lampu & Pendingin"
    if any(k in low for k in ("mouse pad", "deskmat", "desk mat", "table mat", "kabel", "cable", "bracket", "clamp", "pegboard")):
        return "Aksesoris Meja"
    if any(k in low for k in ("meja", "standing desk", "foot rest", "footrest", "sandaran kaki")):
        return "Meja & Ergonomi"
    if any(k in low for k in ("rak", "laci", "storage", "organizer", "box", "kotak", "dispencer", "dispenser", "tissue", "pen holder", "kosmetik")):
        return "Rak & Organizer"
    return "Meja & Kos"


def build_masalah_dan_keunggulan(
    *,
    clean_name: str,
    category: str,
    sales: str,
    shop_name: str,
) -> str:
    """Buat ringkasan masalah & keunggulan awal yang kaya konteks untuk AI Copywriter."""
    low = clean_name.lower()
    if "charger" in low or "pengisi daya" in low:
        hook = "Colokan sering penuh & ngecas lama; charger ringkas ini bantu isi daya lebih cepat dan meja tetap rapi."
    elif "lampu" in low:
        hook = "Mata gampang lelah saat kerja/belajar malam atau bikin konten; pencahayaan lebih merata dan hemat tempat."
    elif "kabel" in low or "cable" in low:
        hook = "Kabel berantakan di kolong meja bikin risih; solusi rapikan kabel tersembunyi di bawah meja kerja."
    elif "mouse pad" in low or "deskmat" in low or "desk mat" in low or "table mat" in low:
        hook = "Permukaan meja gampang lecet dan mouse kurang mulus; alas meja luas bikin setup kerja/gaming lebih rapi & nyaman."
    elif "bracket" in low or "clamp" in low:
        hook = "Layar laptop/monitor terlalu rendah bikin leher pegal dan makan tempat; pakai arm/clamp biar meja jauh lebih lega."
    elif "foot rest" in low or "footrest" in low or "pijakan kaki" in low or "sandaran kaki" in low:
        hook = "Kaki cepat pegal kalau duduk kerja berjam-jam; sandaran kaki ergonomis bantu posisi duduk lebih santai."
    elif "kipas" in low or "fan" in low:
        hook = "Kamar kos/meja kerja pengap dan gerah; kipas meja ringkas tidak berisik dan hemat ruang."
    elif "speaker" in low or "soundbar" in low:
        hook = "Audio bawaan laptop cempreng saat kerja atau nonton; soundbar desktop ringkas bikin suara lebih nendang tanpa makan tempat."
    elif "stop kontak" in low:
        hook = "Colokan berceceran di atas meja bikin berantakan; stop kontak tanam pop-up bikin meja meeting/kerja terlihat clean."
    elif "meja" in low or "standing desk" in low:
        hook = "Butuh area kerja/belajar yang kokoh tapi tetap muat di kamar kos atau ruangan minimalis; praktis dipakai WFH maupun belajar."
    elif "kosmetik" in low or "make up" in low:
        hook = "Skincare & makeup sering berantakan di meja rias/kos; rak/laci organizer bikin barang tertata rapi, bebas debu, dan mudah dicari."
    elif "tissue" in low or "dispencer" in low or "dispenser" in low:
        hook = "Meja atau sudut kos gampang berantakan sama tisu/gelas; tempat penyimpanan praktis ini bikin sudut ruangan lebih rapi."
    elif "rak" in low or "laci" in low or "storage" in low or "organizer" in low or "pegboard" in low or "pen holder" in low:
        hook = "Alat tulis, dokumen, atau printilan kecil sering tercecer; organizer minimalis ini bikin meja kerja & kamar kos rapi seketika."
    else:
        hook = f"Perlengkapan {category.lower()} praktis untuk bikin meja kerja dan kamar kos lebih rapi serta nyaman dipakai harian."

    proof_parts: list[str] = []
    if sales:
        proof_parts.append(f"terjual {sales}")
    if shop_name:
        proof_parts.append(f"toko {shop_name}")
    proof = f" ({', '.join(proof_parts)})" if proof_parts else ""
    return f"{hook}{proof}."


def parse_shopee_bulk_csv(csv_path: str | Path) -> list[dict[str, str]]:
    """Parse CSV ekspor `LinkProdukSekaligus*.csv` menjadi daftar dict siap masuk `Antrean_Produk`."""
    path = Path(csv_path)
    items: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_name = (row.get("Nama Produk") or "").strip()
            if not raw_name:
                continue
            link_ekstra = (row.get("Link Komisi Ekstra") or "").strip()
            link_produk = (row.get("Link Produk") or "").strip()
            affiliate_link = link_ekstra or link_produk
            if not affiliate_link:
                continue

            clean_name = clean_product_title(raw_name)
            category = classify_category(clean_name)
            price = normalize_shopee_price(row.get("Harga") or "")
            sales = (row.get("Penjualan") or "").strip()
            shop_name = (row.get("Nama Toko") or "").replace("\u00a0", " ").strip()
            comm_pct = (row.get("Komisi hingga") or "").strip()
            comm_rp = (row.get("Komisi") or "").strip()

            masalah = build_masalah_dan_keunggulan(
                clean_name=clean_name,
                category=category,
                sales=sales,
                shop_name=shop_name,
            )

            note_parts: list[str] = []
            if sales:
                note_parts.append(f"Terjual {sales}")
            if shop_name:
                note_parts.append(f"Toko: {shop_name}")
            if comm_pct or comm_rp:
                note_parts.append(f"Komisi s.d. {comm_pct} ({comm_rp})".strip())
            catatan_ai = " | ".join(note_parts)

            items.append(
                {
                    "shopee_id": (row.get("ID Produk") or "").strip(),
                    "kategori": category,
                    "nama_produk": clean_name,
                    "harga_kisaran": price,
                    "masalah_dan_keunggulan": masalah,
                    "link_affiliate": affiliate_link,
                    "catatan_user_untuk_ai": catatan_ai,
                }
            )
    return items


def import_csv_to_sheets(sheets: SheetsClient, csv_path: str | Path) -> dict[str, int]:
    """Impor item CSV ke `Antrean_Produk` dengan deduplikasi `Link_Affiliate` & `Nama_Produk`."""
    parsed = parse_shopee_bulk_csv(csv_path)
    existing = sheets.list_queue_rows()
    existing_links = {r.link_affiliate.strip().lower() for r in existing if r.link_affiliate}
    existing_names = {r.nama_produk.strip().lower() for r in existing if r.nama_produk}

    added = 0
    skipped = 0
    now_str = now_wib_str()

    for item in parsed:
        link_key = item["link_affiliate"].strip().lower()
        name_key = item["nama_produk"].strip().lower()
        if link_key in existing_links or name_key in existing_names:
            skipped += 1
            continue

        sheets.append_queue_product(
            status="PENDING",
            kategori=item["kategori"],
            nama_produk=item["nama_produk"],
            harga_kisaran=item["harga_kisaran"],
            masalah_dan_keunggulan=item["masalah_dan_keunggulan"],
            link_affiliate=item["link_affiliate"],
            catatan_user_untuk_ai=item["catatan_user_untuk_ai"],
            sumber="CSV_BULK_IMPORT",
            catatan_agent=f"[{now_str}] Diimpor dari CSV massal Shopee (ID {item['shopee_id']})",
        )
        existing_links.add(link_key)
        existing_names.add(name_key)
        added += 1

    return {"total_csv": len(parsed), "added": added, "skipped": skipped}

"""Agent 2 — Copywriter & Quality Control (QC).

Menggunakan Gemini (dimulai dari `gemini-4-hydrogen` dengan degradasi otomatis
ke model gratis tertinggi) serta menyerap laporan riset terbaru dari Agent 6
(`Audit_Riset_Threads`) untuk membuat:
1. Utas utama Meta Threads (gaya ngobrol human, DILARANG mengandung link/frasa robotik).
2. Komentar balasan Threads berisi link Shopee Affiliate.
3. Draf kurasi X siap copy-paste (dikirim ke Telegram).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from src.gemini_client import GeminiCallMeta, GeminiClient
from src.sheets_client import ProductRow

URL_LEAK_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|\b(?:s\.shopee\.co\.id|shopee\.co\.id|shp\.ee|bit\.ly|lynk\.id|tinyurl\.com|s\.id)/\S*)",
    re.IGNORECASE,
)

DEFAULT_ROBOTIC_PHRASES: tuple[str, ...] = (
    "jangan lewatkan",
    "miliki sekarang juga",
    "buruan checkout sekarang",
    "dapatkan segera",
    "solusi terbaik untuk anda",
    "klik link di bio sekarang",
    "penawaran terbatas",
)


class CopywritingDraft(BaseModel):
    threads_main_post: str = Field(
        description="Postingan utama Meta Threads (maks 460 karakter, gaya ngobrol teman, TANPA link/URL sama sekali)"
    )
    threads_reply_text: str = Field(
        description="Komentar balasan singkat untuk utas Threads yang menyertakan link Shopee Affiliate"
    )
    x_draft_post: str = Field(
        description="Draf postingan X gaya kurasi ringkas & elegan (maks 270 karakter termasuk link)"
    )


@dataclass(frozen=True)
class CopywritingResult:
    draft: CopywritingDraft
    meta: GeminiCallMeta
    qc_passed: bool
    qc_notes: str


def strip_leaked_links(text: str) -> tuple[str, bool]:
    """Hapus URL/link yang bocor dari utas utama Threads secara deterministik."""
    if not URL_LEAK_PATTERN.search(text):
        return text.strip(), False
    cleaned = URL_LEAK_PATTERN.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(" \n\t-•"), True


def parse_banned_phrases(raw_pantangan: str | None) -> list[str]:
    phrases = list(DEFAULT_ROBOTIC_PHRASES)
    if raw_pantangan:
        for part in re.split(r"[;\n,]+", raw_pantangan):
            clean = part.strip().lower()
            if len(clean) >= 4 and clean not in phrases:
                phrases.append(clean)
    return phrases


def find_robotic_phrases(text: str, banned_phrases: list[str]) -> list[str]:
    low = (text or "").lower()
    return [phrase for phrase in banned_phrases if phrase in low]


def run_qc_and_sanitize(
    draft: CopywritingDraft,
    affiliate_link: str,
    *,
    banned_phrases: list[str] | None = None,
) -> tuple[CopywritingDraft, bool, str]:
    """Pastikan utas utama Threads bebas link bocor & frasa robotik, dan reply berisi link affiliate."""
    notes: list[str] = []
    qc_clean = True

    main_post = draft.threads_main_post.strip()
    main_post, leaked = strip_leaked_links(main_post)
    if leaked:
        qc_clean = False
        notes.append("Link terdeteksi di utas utama Threads dan dibersihkan oleh QC")

    robotic_hits = find_robotic_phrases(
        main_post, banned_phrases or list(DEFAULT_ROBOTIC_PHRASES)
    )
    if robotic_hits:
        qc_clean = False
        notes.append(f"Terdeteksi frasa kaku/iklan ({', '.join(robotic_hits[:3])})")

    if len(main_post) > 495:
        main_post = main_post[:492].rstrip() + "..."
        notes.append("Utas utama dipangkas <= 495 karakter")

    reply_text = draft.threads_reply_text.strip()
    if affiliate_link not in reply_text:
        reply_text = f"{reply_text}\n\nCek detail produknya di sini ya: {affiliate_link}".strip()
        notes.append("Link affiliate disisipkan ke balasan komentar Threads")

    if len(reply_text) > 495:
        allowed = max(40, 490 - len(affiliate_link) - 2)
        prefix = reply_text.replace(affiliate_link, "").strip()[:allowed].rstrip()
        reply_text = f"{prefix}\n{affiliate_link}".strip()
        notes.append("Teks balasan dirapikan <= 495 karakter")

    x_post = draft.x_draft_post.strip()
    if affiliate_link not in x_post:
        x_post = f"{x_post}\n\n{affiliate_link}".strip()
        notes.append("Link affiliate ditambahkan ke draf X")

    sanitized = CopywritingDraft(
        threads_main_post=main_post,
        threads_reply_text=reply_text,
        x_draft_post=x_post,
    )
    if not notes:
        notes.append("QC OK: bebas link bocor, gaya human terjaga & batas karakter aman")
    return sanitized, qc_clean, "; ".join(notes)


def generate_copywriting(
    gemini: GeminiClient,
    product: ProductRow,
    sheet_config: dict[str, str],
    *,
    audit_report: dict[str, str] | None = None,
) -> CopywritingResult:
    """Hasilkan draf Threads + balasan link + draf X dan jalankan QC."""
    niche = sheet_config.get(
        "Niche_Utama", "Gadget Meja Kerja & Perlengkapan Kos/Rumah Tangga"
    )
    gaya_threads = sheet_config.get(
        "Gaya_Bahasa_Threads",
        "Santai, jujur, conversational, seperti teman merekomendasikan barang (bukan bahasa iklan kaku)",
    )
    gaya_x = sheet_config.get(
        "Gaya_Bahasa_X",
        "Kurasi ringkas, to-the-point, pakai poin manfaat nyata, elegan & informatif",
    )
    panggilan = sheet_config.get("Persona_Panggilan", "aku / kamu")
    link_bio = sheet_config.get("Link_Bio_Landing", "").strip()

    audit_block = ""
    banned_phrases = parse_banned_phrases(
        (audit_report or {}).get("Pantangan_QC_Baru")
    )
    if audit_report:
        audit_block = (
            "\n\nBEKAL RISET THREADS TERBARU (WAJIB DIIKUTI):\n"
            f"- Pola Hook Engagement Tinggi:\n{audit_report.get('Pola_Hook_Terbaik', '-')}\n"
            f"- Pola Kalimat Human:\n{audit_report.get('Pola_Kalimat_Human', '-')}\n"
            f"- Pola CTA ke Reply:\n{audit_report.get('Pola_CTA_Link_Reply', '-')}\n"
            f"- Kata/Gaya Terlarang (Pantangan QC): {', '.join(banned_phrases[:15])}\n"
        )

    system_prompt = (
        "Kamu adalah Copywriter Affiliate spesialis Meta Threads dan X di Indonesia.\n"
        f"Niche utama: {niche}\n"
        f"Gaya bahasa Threads: {gaya_threads}\n"
        f"Gaya bahasa X: {gaya_x}\n"
        f"Sapaan: {panggilan}\n\n"
        "ATURAN KETAT:\n"
        "1. `threads_main_post`: DILARANG KERAS menulis URL/link apa pun (termasuk shopee.co.id, s.shopee, bit.ly, dll). "
        "Mulai dengan hook masalah sehari-hari yang relate, ceritakan solusi jujurnya dengan bahasa sangat human, akhiri dengan ajakan ringan cek komentar/reply. Maks 450 karakter.\n"
        "2. `threads_reply_text`: Komentar balasan ramah dari penulis yang menyertakan `link_affiliate` secara natural.\n"
        "3. `x_draft_post`: Gaya kurasi X yang ringkas, pakai 2-3 poin singkat + harga kisaran (jika ada) + `link_affiliate`. Maks 270 karakter."
        f"{audit_block}"
    )

    payload = {
        "kategori": product.kategori,
        "nama_produk": product.nama_produk,
        "harga_kisaran": product.harga_kisaran or "terjangkau",
        "masalah_dan_keunggulan": product.masalah_dan_keunggulan,
        "link_affiliate": product.link_affiliate,
        "catatan_khusus_user": product.catatan_user_untuk_ai,
        "link_bio_landing": link_bio,
    }

    user_prompt = (
        "Buatkan paket copywriting untuk produk berikut:\n"
        f"{payload}\n"
        "Ingat: `threads_main_post` TIDAK BOLEH mengandung URL/domain ataupun bahasa iklan kaku ala robot."
    )

    raw_draft, meta = gemini.generate_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_model=CopywritingDraft,
        temperature=0.75,
    )

    sanitized, qc_ok, qc_note = run_qc_and_sanitize(
        raw_draft, product.link_affiliate, banned_phrases=banned_phrases
    )

    # Jika QC mendeteksi link bocor atau frasa robotik, beri 1 kesempatan regenerasi QC
    if not qc_ok:
        try:
            retry_draft, retry_meta = gemini.generate_structured(
                system_prompt=system_prompt,
                user_prompt=(
                    f"{user_prompt}\n\nPERINGATAN QC ({qc_note}): "
                    "Perbaiki `threads_main_post` agar bebas URL sama sekali DAN tidak memakai kalimat iklan kaku. "
                    "Letakkan link HANYA di `threads_reply_text` dan `x_draft_post`."
                ),
                response_model=CopywritingDraft,
                temperature=0.5,
            )
            sanitized2, qc_ok2, qc_note2 = run_qc_and_sanitize(
                retry_draft, product.link_affiliate, banned_phrases=banned_phrases
            )
            return CopywritingResult(
                draft=sanitized2,
                meta=retry_meta,
                qc_passed=qc_ok2,
                qc_notes=qc_note2,
            )
        except Exception:
            pass

    return CopywritingResult(
        draft=sanitized,
        meta=meta,
        qc_passed=qc_ok,
        qc_notes=qc_note,
    )

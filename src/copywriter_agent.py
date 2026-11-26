"""Agent 2 — Copywriter & Quality Control (QC) Anti-Bot.

Menghasilkan konten Meta Threads dan draf kurasi X yang terasa seperti tulisan
manusia asli:
1. Context-aware anti-repetition: membaca hingga 10 naskah terakhir dari `Log_Performa`
   dan melarang pengulangan kalimat pembuka/hook yang sama.
2. Rotasi angle dinamis (`honest_micro_review`, `problem_specific_story`,
   `comparison`, `casual_thought`) + mode `ORGANIK_OPINI` / `ORGANIK_TIPS` / `ORGANIK_DISKUSI`.
3. QC ketat berbasis regex/keyword dengan maksimal 3x regenerasi otomatis jika
   draf mengandung frasa klise AI, penomoran `1/2` / `2/2`, hashtag, bullet kaku,
   kelebihan emoji, URL bocor, atau kapitalisasi tidak konsisten.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from src.config import (
    AFFILIATE_CONTENT_TYPE,
    is_organic_content_type,
    normalize_content_type,
)
from src.gemini_client import GeminiCallMeta, GeminiClient
from src.sheets_client import ProductRow

URL_LEAK_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|\b(?:s\.shopee\.co\.id|shopee\.co\.id|shp\.ee|bit\.ly|lynk\.id|tinyurl\.com|s\.id)/\S*)",
    re.IGNORECASE,
)
THREAD_NUMBERING_PATTERN = re.compile(r"(?:\b[12]\s*/\s*2\b|\(\s*[12]\s*/\s*2\s*\))")
HASHTAG_PATTERN = re.compile(r"(?:^|\s)#[A-Za-z0-9_]+")
BULLET_LIST_PATTERN = re.compile(r"(?m)^\s*(?:[-•*]|\d+[.)])\s+")
EMOJI_PATTERN = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
SHOUT_CAPS_PATTERN = re.compile(r"\b[A-Z]{4,}\s+[A-Z]{4,}\b")
LOWERCASE_SENTENCE_START_PATTERN = re.compile(r"(?:^|[.!?]\s+)([a-z]{3,})")

DEFAULT_ROBOTIC_PHRASES: tuple[str, ...] = (
    "sering kesel gak sih",
    "sering kesel nggak sih",
    "pernah gak sih",
    "pernah nggak sih",
    "jujur seneng banget",
    "tanpa bikin kantong jebol",
    "bikin mundur teratur",
    "spill link",
    "auto rapi",
    "wajib punya",
    "solusi terbaik",
    "langsung check out",
    "langsung checkout",
    "jangan lewatkan",
    "miliki sekarang juga",
    "buruan checkout sekarang",
    "dapatkan segera",
    "solusi terbaik untuk anda",
    "klik link di bio sekarang",
    "penawaran terbatas",
)

VARIED_LINK_REPLY_TEMPLATES: tuple[str, ...] = (
    "Barangkali ada yang lagi nyari juga, barangnya aku simpan di sini: {link}",
    "Kalau mau lihat detail spesifikasi atau ulasan pembelinya, ada di tautan ini: {link}",
    "Buat pembanding harga atau cek varian warnanya, bisa intip di sini: {link}",
    "Detail barang dan tokonya kebetulan masih ada di catatan ini: {link}",
)

ANGLE_SPECS: tuple[tuple[str, str], ...] = (
    (
        "honest_micro_review",
        "Honest Micro-Review: Tulis pengalaman pakai singkat yang realistis dan WAJIB sertakan 1 kekurangan minor/trade-off nyata (misal: kabel pas-pasan, bahan plastik standar, atau perlu penyesuaian posisi) agar terdengar seperti review manusia asli.",
    ),
    (
        "problem_specific_story",
        "Problem-Specific Story: Ceritakan satu kejadian kecil sehari-hari yang spesifik (bukan keluhan brosur) lalu jelaskan kenapa barang ini membantu tanpa melebih-lebihkan.",
    ),
    (
        "comparison",
        "Comparison: Bandingkan secara objektif opsi versi hemat vs versi mahal yang beredar, jelaskan kapan versi hemat sudah cukup dan kapan tidak.",
    ),
    (
        "casual_thought",
        "Casual Thought / Question: Cukup 1 sampai 3 kalimat pendek yang santai dan memancing komentar/diskusi alami, tanpa nada promosi.",
    ),
)

MAX_QC_RETRIES = 3


class CopywritingDraft(BaseModel):
    threads_main_post: str = Field(
        description="Postingan utama Meta Threads (2-5 kalimat, gaya ngobrol rapi, maks 1 emoji, tanpa hashtag, tanpa bullet list, TANPA link/URL)"
    )
    threads_reply_text: str = Field(
        description="Komentar balasan natural berisi link affiliate (kosong `\"\"` jika Tipe_Konten bertipe ORGANIK_*)"
    )
    x_draft_post: str = Field(
        description="Draf postingan X gaya kurasi ringkas & informatif (maks 270 karakter)"
    )


@dataclass(frozen=True)
class CopywritingResult:
    draft: CopywritingDraft
    meta: GeminiCallMeta
    qc_passed: bool
    qc_notes: str
    angle_used: str = ""


def strip_leaked_links(text: str) -> tuple[str, bool]:
    """Hapus URL/link yang bocor dari utas utama Threads secara deterministik."""
    if not URL_LEAK_PATTERN.search(text):
        return text.strip(), False
    cleaned = URL_LEAK_PATTERN.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(" \n\t-•"), True


def extract_opening_hook(text: str) -> str:
    """Ambil kalimat/baris pembuka dari draf Threads untuk deteksi repetisi."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return ""
    first_sentence = re.split(r"(?<=[.!?])\s+|\n", cleaned, maxsplit=1)[0].strip()
    return first_sentence[:140]


def extract_recent_hooks(recent_posts: list[dict[str, str]] | None, limit: int = 10) -> list[str]:
    """Ekstrak daftar kalimat pembuka dari hingga 10 naskah terakhir di `Log_Performa`."""
    if not recent_posts:
        return []
    hooks: list[str] = []
    for item in recent_posts[:limit]:
        raw = item.get("draft_threads") or item.get("Draft_Threads") or ""
        hook = extract_opening_hook(raw)
        if hook and hook not in hooks:
            hooks.append(hook)
    return hooks


def _has_repetitive_opening(main_post: str, recent_hooks: list[str]) -> str | None:
    if not recent_hooks:
        return None
    current_hook = extract_opening_hook(main_post).lower()
    current_words = re.findall(r"[a-z0-9]+", current_hook)[:5]
    if len(current_words) < 3:
        return None
    for prev in recent_hooks:
        prev_words = re.findall(r"[a-z0-9]+", prev.lower())[:5]
        if len(prev_words) >= 3 and current_words[:4] == prev_words[:4]:
            return prev
    return None


def select_dynamic_angle(
    product: ProductRow,
    recent_posts: list[dict[str, str]] | None = None,
) -> tuple[str, str]:
    """Pilih angle penulisan secara dinamis agar pola posting tidak seragam."""
    tipe = normalize_content_type(getattr(product, "tipe_konten", AFFILIATE_CONTENT_TYPE))
    if tipe == "ORGANIK_OPINI":
        return (
            "organik_opini",
            "Opini Kasual / Cerita Relatable: Tulis sudut pandang santai soal kebiasaan meja kerja atau hidup anak kos. DILARANG ada ajakan beli atau link.",
        )
    if tipe == "ORGANIK_TIPS":
        return (
            "organik_tips",
            "Tips Praktis Singkat: Bagikan 1 trik simpel sehari-hari dalam paragraf percakapan mengalir (tanpa bullet list bernomor). DILARANG ada link atau promosi.",
        )
    if tipe == "ORGANIK_DISKUSI":
        return (
            "organik_diskusi",
            "Pertanyaan Pemancing Diskusi: Tulis 1-3 kalimat pendek berisi observasi ringan + pertanyaan terbuka yang bikin orang ingin membalas. DILARANG ada link.",
        )

    offset = len(recent_posts or [])
    if product.product_id.isdigit():
        offset += int(product.product_id)
    return ANGLE_SPECS[offset % len(ANGLE_SPECS)]


def parse_banned_phrases(raw_pantangan: str | None) -> list[str]:
    phrases = list(DEFAULT_ROBOTIC_PHRASES)
    if raw_pantangan:
        for part in re.split(r"[;\n,]+", raw_pantangan):
            clean = part.strip().lower()
            if len(clean) >= 4 and clean not in phrases:
                phrases.append(clean)
    return phrases


def find_robotic_phrases(text: str, banned_phrases: list[str]) -> list[str]:
    normalized = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return [phrase for phrase in banned_phrases if phrase in normalized]


def run_qc_and_sanitize(
    draft: CopywritingDraft,
    affiliate_link: str,
    *,
    banned_phrases: list[str] | None = None,
    tipe_konten: str = AFFILIATE_CONTENT_TYPE,
    recent_hooks: list[str] | None = None,
) -> tuple[CopywritingDraft, bool, str]:
    """Validasi ketat anti-bot pada draf Threads & X sebelum publikasi."""
    notes: list[str] = []
    qc_clean = True
    normalized_type = normalize_content_type(tipe_konten)
    organic = is_organic_content_type(normalized_type)

    main_post = draft.threads_main_post.strip()
    main_post, leaked = strip_leaked_links(main_post)
    if leaked:
        qc_clean = False
        notes.append("Link terdeteksi di utas utama Threads dan dibersihkan oleh QC")

    if THREAD_NUMBERING_PATTERN.search(main_post) or THREAD_NUMBERING_PATTERN.search(
        draft.threads_reply_text
    ):
        qc_clean = False
        main_post = THREAD_NUMBERING_PATTERN.sub("", main_post).strip()
        notes.append("Penomoran robotik 1/2 atau 2/2 ditolak QC")

    if HASHTAG_PATTERN.search(main_post):
        qc_clean = False
        main_post = re.sub(r"\s*#[A-Za-z0-9_]+", "", main_post).strip()
        notes.append("Hashtag dilarang di Threads")

    if BULLET_LIST_PATTERN.search(main_post):
        qc_clean = False
        notes.append("Bullet list kaku terdeteksi di utas utama Threads")

    emojis = EMOJI_PATTERN.findall(main_post)
    if len(emojis) > 1:
        qc_clean = False
        notes.append(f"Terlalu banyak emoji ({len(emojis)} > maks 1)")

    if SHOUT_CAPS_PATTERN.search(main_post) or LOWERCASE_SENTENCE_START_PATTERN.search(
        main_post
    ):
        qc_clean = False
        notes.append("Kapitalisasi kalimat tidak konsisten/terlalu hype")

    robotic_hits = find_robotic_phrases(
        main_post, banned_phrases or list(DEFAULT_ROBOTIC_PHRASES)
    )
    if robotic_hits:
        qc_clean = False
        notes.append(f"Terdeteksi frasa kaku/iklan ({', '.join(robotic_hits[:3])})")

    repeated_hook = _has_repetitive_opening(main_post, recent_hooks or [])
    if repeated_hook:
        qc_clean = False
        notes.append(f"Pembuka mirip postingan sebelumnya ('{repeated_hook[:50]}')")

    if len(main_post) > 495:
        main_post = main_post[:492].rstrip() + "..."
        notes.append("Utas utama dipangkas <= 495 karakter")

    if organic:
        reply_text = ""
        if draft.threads_reply_text.strip():
            notes.append("Konten organik wajib tanpa auto-reply link; balasan dikosongkan")
        x_post, x_leaked = strip_leaked_links(draft.x_draft_post.strip())
        if x_leaked:
            notes.append("Link dihapus dari draf X karena tipe konten ORGANIK")
    else:
        reply_text = THREAD_NUMBERING_PATTERN.sub("", draft.threads_reply_text.strip()).strip()
        if affiliate_link and affiliate_link not in reply_text:
            if not reply_text or "ini ya link" in reply_text.lower():
                reply_text = random.choice(VARIED_LINK_REPLY_TEMPLATES).format(
                    link=affiliate_link
                )
            else:
                reply_text = f"{reply_text}\n\n{affiliate_link}".strip()
            notes.append("Link affiliate disisipkan ke balasan komentar Threads")

        if len(reply_text) > 495 and affiliate_link:
            allowed = max(40, 490 - len(affiliate_link) - 2)
            prefix = reply_text.replace(affiliate_link, "").strip()[:allowed].rstrip()
            reply_text = f"{prefix}\n{affiliate_link}".strip()
            notes.append("Teks balasan dirapikan <= 495 karakter")

        x_post = draft.x_draft_post.strip()
        if affiliate_link and affiliate_link not in x_post:
            x_post = f"{x_post}\n\n{affiliate_link}".strip()
            notes.append("Link affiliate ditambahkan ke draf X")

    sanitized = CopywritingDraft(
        threads_main_post=main_post,
        threads_reply_text=reply_text,
        x_draft_post=x_post,
    )
    if not notes:
        notes.append("QC OK: bebas klise AI, gaya human rapi & sesuai aturan anti-bot")
    return sanitized, qc_clean, "; ".join(notes)


def generate_copywriting(
    gemini: GeminiClient,
    product: ProductRow,
    sheet_config: dict[str, str],
    *,
    audit_report: dict[str, str] | None = None,
    recent_posts: list[dict[str, str]] | None = None,
) -> CopywritingResult:
    """Panggil AI untuk menghasilkan draf humanized Threads + X dengan maks 3x QC retry."""
    niche = sheet_config.get(
        "Niche_Utama", "Gadget Meja Kerja & Perlengkapan Kos/Rumah Tangga"
    )
    gaya_threads = sheet_config.get(
        "Gaya_Bahasa_Threads",
        "Santai, jujur, conversational, seperti teman ngobrol (bukan bahasa iklan kaku)",
    )
    gaya_x = sheet_config.get(
        "Gaya_Bahasa_X",
        "Kurasi ringkas, to-the-point, informatif & natural",
    )
    panggilan = sheet_config.get("Persona_Panggilan", "aku / kamu")
    tipe_konten = normalize_content_type(
        getattr(product, "tipe_konten", AFFILIATE_CONTENT_TYPE)
    )
    organic = is_organic_content_type(tipe_konten)

    recent_hooks = extract_recent_hooks(recent_posts, limit=10)
    angle_name, angle_instruction = select_dynamic_angle(product, recent_posts)

    banned_phrases = parse_banned_phrases(
        (audit_report or {}).get("Pantangan_QC_Baru")
    )

    anti_repeat_block = ""
    if recent_hooks:
        hooks_lines = "\n".join(f"- \"{h}\"" for h in recent_hooks)
        anti_repeat_block = (
            "\n\nLARANGAN PEMBUKA BERULANG (10 postingan terakhir sudah memakai hook ini):\n"
            f"{hooks_lines}\n"
            "WAJIB buka dengan kalimat dan pola pikir yang BERBEDA total dari daftar di atas."
        )

    audit_block = ""
    if audit_report:
        audit_block = (
            "\n\nBEKAL RISET THREADS TERBARU:\n"
            f"- Pola Hook Engagement Tinggi: {audit_report.get('Pola_Hook_Terbaik', '-')}\n"
            f"- Pola Kalimat Human: {audit_report.get('Pola_Kalimat_Human', '-')}\n"
        )

    if organic:
        type_rules = (
            f"TIPE KONTEN SAAT INI: {tipe_konten} (100% ORGANIK TANPA JUALAN/LINK).\n"
            "- `threads_main_post`: Tulis 1 utas tunggal (2-5 kalimat untuk opini/tips atau 1-3 kalimat untuk diskusi) yang terasa seperti curhat/obrolan manusia asli sesuai niche.\n"
            "- DILARANG keras menyebut merek promosi, harga, CTA cek komentar, atau menyisipkan URL.\n"
            "- `threads_reply_text`: WAJIB string kosong `\"\"`.\n"
            "- `x_draft_post`: Opini/tips ringkas versi X tanpa link affiliate."
        )
    else:
        type_rules = (
            "TIPE KONTEN SAAT INI: SOFT_AFFILIATE (ulasan ringan/cerita manusiawi).\n"
            "- `threads_main_post`: DILARANG menulis URL/link apa pun. TIDAK wajib menulis 'link di reply bawah ya' pada setiap postingan — biarkan utas utama berdiri natural seperti review/cerita murni.\n"
            "- `threads_reply_text`: 1 kalimat santai dan variatif dari penulis yang menyertakan `link_affiliate` (JANGAN pakai kalimat template 'Ini ya link...').\n"
            "- `x_draft_post`: Kurasi ringkas X (maks 270 karakter) + `link_affiliate`."
        )

    system_prompt = (
        "Kamu adalah Penulis Konten Meta Threads dan X yang menulis layaknya pengguna manusia biasa di Indonesia.\n"
        f"Niche utama: {niche}\n"
        f"Gaya bahasa Threads: {gaya_threads}\n"
        f"Gaya bahasa X: {gaya_x}\n"
        f"Sapaan: {panggilan}\n\n"
        f"ANGLE PENULISAN WAJIB CYCLE INI ({angle_name}):\n{angle_instruction}\n\n"
        f"{type_rules}\n\n"
        "STYLE GUIDE & LARANGAN KERAS (JIKA MELANGGAR AKAN DITOLAK QC):\n"
        "1. Bahasa Indonesia percakapan sehari-hari yang santai namun RAPI (gunakan huruf kapital normal yang konsisten di awal setiap kalimat, hindari huruf KAPITAL semua).\n"
        "2. Panjang 2-5 kalimat (atau 1-3 kalimat untuk angle diskusi), bervariasi ritmenya.\n"
        "3. Maksimal 1 emoji (boleh 0 emoji). DILARANG hashtag (#), DILARANG bullet list (-/•/1.), DILARANG penomoran utas seperti `1/2` atau `2/2`.\n"
        f"4. DILARANG frasa klise AI/iklan: {', '.join(banned_phrases)}."
        f"{anti_repeat_block}"
        f"{audit_block}"
    )

    payload = {
        "tipe_konten": tipe_konten,
        "angle": angle_name,
        "kategori": product.kategori,
        "topik_atau_produk": product.nama_produk,
        "harga_kisaran": "" if organic else (product.harga_kisaran or "terjangkau"),
        "konteks_masalah_atau_poin": product.masalah_dan_keunggulan,
        "link_affiliate": "" if organic else product.link_affiliate,
        "catatan_khusus_user": product.catatan_user_untuk_ai,
    }

    base_user_prompt = (
        "Buatkan draf konten Threads & X berdasarkan data berikut:\n"
        f"{payload}\n"
        "Pastikan tulisan terdengar seperti pikiran/pengalaman manusia asli, bebas klise AI, "
        "dan mengikuti seluruh aturan Style Guide."
    )

    best_sanitized: CopywritingDraft | None = None
    best_meta: GeminiCallMeta | None = None
    best_qc_note = ""
    current_prompt = base_user_prompt

    for attempt in range(MAX_QC_RETRIES + 1):
        temp = 0.78 if attempt == 0 else max(0.35, 0.7 - 0.12 * attempt)
        try:
            raw_draft, meta = gemini.generate_structured(
                system_prompt=system_prompt,
                user_prompt=current_prompt,
                response_model=CopywritingDraft,
                temperature=temp,
            )
        except Exception:
            if best_sanitized is not None and best_meta is not None:
                return CopywritingResult(
                    draft=best_sanitized,
                    meta=best_meta,
                    qc_passed=False,
                    qc_notes=best_qc_note,
                    angle_used=angle_name,
                )
            raise

        sanitized, qc_ok, qc_note = run_qc_and_sanitize(
            raw_draft,
            product.link_affiliate,
            banned_phrases=banned_phrases,
            tipe_konten=tipe_konten,
            recent_hooks=recent_hooks,
        )
        best_sanitized = sanitized
        best_meta = meta
        best_qc_note = qc_note

        if qc_ok:
            note_with_angle = f"[Angle: {angle_name}] {qc_note}"
            if attempt > 0:
                note_with_angle = f"[Lolos QC setelah {attempt}x retry | Angle: {angle_name}] {qc_note}"
            return CopywritingResult(
                draft=sanitized,
                meta=meta,
                qc_passed=True,
                qc_notes=note_with_angle,
                angle_used=angle_name,
            )

        current_prompt = (
            f"{base_user_prompt}\n\n"
            f"REVISI WAJIB QC (percobaan ke-{attempt + 1}/{MAX_QC_RETRIES}): {qc_note}.\n"
            "Perbaiki naskah secara menyeluruh: hindari seluruh frasa pantangan, jangan pakai hashtag/bullet/`1/2`, "
            "gunakan kapitalisasi awal kalimat yang rapi, maksimal 1 emoji, dan gunakan pembuka yang benar-benar baru."
        )

    assert best_sanitized is not None and best_meta is not None
    return CopywritingResult(
        draft=best_sanitized,
        meta=best_meta,
        qc_passed=False,
        qc_notes=f"[Angle: {angle_name} | Gagal QC setelah {MAX_QC_RETRIES}x retry] {best_qc_note}",
        angle_used=angle_name,
    )

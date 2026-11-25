"""Agent 6 — Threads Trend, Hook & Human-Copy Researcher.

Tugas (berjalan tiap 3 hari sekali & bisa dipicu manual):
1. Berseluncur di internet via Gemini Google Search Grounding untuk mempelajari
   pola postingan akun affiliate Threads Indonesia dengan engagement tinggi,
   formula hook pembuka, gaya kalimat *human* (anti-robot), dan penempatan CTA.
2. Membaca performa postingan akun sendiri (`@bilditmoy`) via Threads API
   (jika `threads_manage_insights` aktif) serta `keyword_search` (jika disetujui Meta).
3. Membaca contoh postingan viral yang dikumpulkan user di tab `Referensi_Viral_Threads`.
4. Menyusun Laporan Audit Riset ke tab `Audit_Riset_Threads` (otomatis menjadi
   bekal sistem prompt & aturan QC Agent 2 Copywriter) serta mengirim ringkasan ke Telegram.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote_plus

import requests
from pydantic import BaseModel, Field

from src.config import Settings, now_wib_str, sheet_url
from src.gemini_client import GeminiClient
from src.publishers import THREADS_API_BASE, send_telegram_message
from src.sheets_client import SheetsClient

DEFAULT_RESEARCH_KEYWORDS = (
    "racun shopee meja kerja threads",
    "perlengkapan kos shopee affiliate threads",
    "link di komentar threads shopee",
)


class ThreadsAuditReport(BaseModel):
    pola_hook_terbaik: str = Field(
        description="4-5 formula hook pembuka paling memancing berhenti scroll & komentar (pakai poin bernomor)"
    )
    pola_kalimat_human: str = Field(
        description="Panduan diksi, panjang kalimat, ritme paragraf, dan gaya storytelling jujur agar terasa seperti manusia sungguhan"
    )
    pola_cta_link_reply: str = Field(
        description="3-4 contoh kalimat transisi halus untuk mengarahkan pembaca mengecek link di balasan komentar tanpa terasa spam"
    )
    pantangan_qc: list[str] = Field(
        description="Daftar 6-12 kata/frasa klise, kaku, atau berbau iklan/AI yang harus diblokir QC (contoh: 'jangan lewatkan', 'miliki sekarang')"
    )
    ringkasan_eksekutif: str = Field(
        description="Ringkasan audit 3-5 kalimat berisi temuan utama agar mudah dibaca user di Telegram & Sheets"
    )


def fetch_web_trend_snippets(
    *,
    queries: tuple[str, ...] = (
        "threads shopee affiliate hook viral indonesia",
        "strategi engagement threads jualan affiliate",
    ),
    http_get: Any = requests.get,
) -> list[str]:
    """Ambil cuplikan artikel/diskusi publik dari Google News RSS (gratis, tanpa API key)."""
    snippets: list[str] = []
    for q in queries[:2]:
        try:
            url = (
                f"https://news.google.com/rss/search?q={quote_plus(q)}"
                "&hl=id&gl=ID&ceid=ID:id"
            )
            resp = http_get(url, timeout=15)
            if resp.status_code != 200 or not getattr(resp, "text", ""):
                continue
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item")[:5]:
                title = (item.findtext("title") or "").strip()
                desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip()
                desc = re.sub(r"\s+", " ", desc)
                if title:
                    snippets.append(f"{title} — {desc[:160]}" if desc else title)
        except Exception:
            continue
    return snippets[:10]


def fetch_own_threads_performance(
    *,
    user_id: str,
    access_token: str,
    limit: int = 10,
    http_get: Any = requests.get,
) -> list[dict[str, Any]]:
    """Ambil postingan terbaru akun sendiri + metrik insights jika izin tersedia."""
    if not user_id or not access_token:
        return []
    posts_summary: list[dict[str, Any]] = []
    try:
        resp = http_get(
            f"{THREADS_API_BASE}/{user_id}/threads",
            params={
                "fields": "id,text,timestamp,permalink",
                "limit": limit,
                "access_token": access_token,
            },
            timeout=25,
        )
        if resp.status_code != 200:
            return []
        posts = resp.json().get("data", []) or []
    except Exception:
        return []

    for p in posts:
        pid = str(p.get("id", "")).strip()
        text = str(p.get("text", "")).strip()
        if not pid or not text:
            continue
        metrics: dict[str, int] = {}
        try:
            ins = http_get(
                f"{THREADS_API_BASE}/{pid}/insights",
                params={
                    "metric": "views,likes,replies,reposts,quotes",
                    "access_token": access_token,
                },
                timeout=15,
            )
            if ins.status_code == 200:
                for m in ins.json().get("data", []) or []:
                    name = m.get("name")
                    vals = m.get("values") or []
                    if name and vals:
                        metrics[name] = int(vals[0].get("value") or 0)
        except Exception:
            pass
        posts_summary.append({"text": text[:280], "metrics": metrics})
    return posts_summary


def fetch_public_keyword_samples(
    *,
    access_token: str,
    keywords: tuple[str, ...] = ("racun shopee", "meja kerja", "anak kos"),
    http_get: Any = requests.get,
) -> list[str]:
    """Coba `GET /keyword_search` resmi Threads (aktif penuh bila App Review disetujui)."""
    if not access_token:
        return []
    samples: list[str] = []
    for kw in keywords[:2]:
        try:
            resp = http_get(
                f"{THREADS_API_BASE}/keyword_search",
                params={
                    "q": kw,
                    "search_type": "TOP",
                    "fields": "id,text,username,timestamp",
                    "limit": 10,
                    "access_token": access_token,
                },
                timeout=20,
            )
            if resp.status_code != 200:
                continue
            for item in resp.json().get("data", []) or []:
                txt = str(item.get("text", "")).strip()
                user = str(item.get("username", "")).strip()
                if txt:
                    samples.append(f"@{user}: {txt[:240]}" if user else txt[:240])
        except Exception:
            continue
    return samples[:10]


def run_trend_researcher(
    *,
    settings: Settings,
    sheets: SheetsClient,
    gemini: GeminiClient,
    http_get: Any = requests.get,
    http_post: Any = requests.post,
) -> dict[str, Any]:
    """Jalankan Agent 6 untuk riset Threads internet, simpan audit ke Sheets, & kirim ke Telegram."""
    sheet_config = sheets.get_settings()
    niche = sheet_config.get(
        "Niche_Utama", "Gadget Meja Kerja & Perlengkapan Kos/Rumah Tangga"
    )
    gs_link = sheet_url(settings.google_sheet_id)

    own_posts = fetch_own_threads_performance(
        user_id=settings.threads_user_id,
        access_token=settings.threads_access_token,
        http_get=http_get,
    )
    api_samples = fetch_public_keyword_samples(
        access_token=settings.threads_access_token,
        http_get=http_get,
    )
    web_snippets = fetch_web_trend_snippets(http_get=http_get)
    sheet_refs = sheets.list_active_viral_references(limit=12)

    sources_used = ["Gemini Search Grounding & Analisis Pola Threads"]
    if web_snippets:
        sources_used.append(f"Web/News Snippets ({len(web_snippets)})")
    if own_posts:
        sources_used.append(f"Performa Akun Sendiri ({len(own_posts)} post)")
    if api_samples:
        sources_used.append(f"Threads Keyword API ({len(api_samples)} sampel)")
    if sheet_refs:
        sources_used.append(f"Referensi Viral Sheet ({len(sheet_refs)} contoh)")

    system_prompt = (
        "Kamu adalah Senior Social Media Strategist & Auditor Konten Meta Threads Indonesia "
        "khusus niche affiliate Shopee.\n"
        f"Niche fokus: {niche}\n"
        "Tugasmu:\n"
        "1. Amati pola akun affiliate Threads Indonesia yang mendapatkan engagement tinggi "
        "(likes, replies, clicks) dari data riset internet, performa akun, dan referensi viral.\n"
        "2. Bedah struktur hook 1-2 kalimat pertama, gaya bahasa yang terasa sangat *human* "
        "(seperti curhat/rekomendasi teman sendiri, ada konteks masalah nyata, bukan bahasa brosur AI), "
        "serta teknik transisi CTA agar orang mau membuka kolom balasan tempat link Shopee ditaruh.\n"
        "3. Identifikasi kata/pola kalimat klise ala robot/iklan yang wajib diblokir oleh modul QC."
    )

    user_prompt = (
        "Lakukan riset & audit pola konten affiliate Threads Indonesia saat ini.\n"
        f"Kata kunci referensi pencarian internet: {', '.join(DEFAULT_RESEARCH_KEYWORDS)}.\n\n"
        f"Cuplikan artikel/diskusi web terbaru:\n{web_snippets[:8]}\n\n"
        f"Data performa postingan akun sendiri:\n{own_posts[:8]}\n\n"
        f"Sampel dari Threads Keyword Search API:\n{api_samples[:8]}\n\n"
        f"Contoh postingan viral yang dikumpulkan user:\n{sheet_refs[:8]}\n\n"
        "Susun Laporan Audit Riset yang konkret, praktis, dan langsung bisa dijadikan panduan "
        "sistem prompt bagi Agent Copywriter dan daftar pantangan bagi Agent QC."
    )

    try:
        report, meta = gemini.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=ThreadsAuditReport,
            temperature=0.5,
            use_google_search=True,
        )
    except Exception as exc:
        send_telegram_message(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            html_text=(
                f"⚠️ <b>Agent 6 (Riset Threads) Gagal Sementara:</b>\n"
                f"<code>{html.escape(str(exc)[:500])}</code>\n\n"
                f"📊 <a href=\"{html.escape(gs_link)}\">Buka Google Sheets</a>"
            ),
            http_post=http_post,
        )
        raise

    now_str = now_wib_str()
    pantangan_str = "; ".join(p.strip() for p in report.pantangan_qc if p.strip())
    sumber_str = " + ".join(sources_used)
    model_label = meta.display_label()

    sheets.append_audit_report(
        {
            "Tanggal_Audit_WIB": now_str,
            "Sumber_Riset": sumber_str,
            "Pola_Hook_Terbaik": report.pola_hook_terbaik,
            "Pola_Kalimat_Human": report.pola_kalimat_human,
            "Pola_CTA_Link_Reply": report.pola_cta_link_reply,
            "Pantangan_QC_Baru": pantangan_str,
            "Ringkasan_Eksekutif": report.ringkasan_eksekutif,
            "Status_Feed_Copywriter": "AKTIF_DIPAKAI",
            "Model_AI_Dipakai": model_label,
        }
    )

    q_warn = meta.quota_warning_message()
    quota_block = (
        f"\n⚠️ <b>PERINGATAN KUOTA GEMINI:</b> {html.escape(q_warn)}\n"
        if q_warn
        else ""
    )

    tg_html = (
        f"🔎 <b>LAPORAN AUDIT AGENT 6 — Riset Threads (Tiap 3 Hari)</b>\n"
        f"<b>Waktu:</b> {html.escape(now_str)}\n"
        f"<b>Model AI:</b> <code>{html.escape(model_label)}</code>\n"
        f"<b>Sumber:</b> {html.escape(sumber_str)}\n"
        f"{quota_block}\n"
        f"<b>Ringkasan Temuan:</b>\n{html.escape(report.ringkasan_eksekutif)}\n\n"
        f"<b>Pola Hook Terbaik (Di-feed ke Copywriter):</b>\n"
        f"<pre>{html.escape(report.pola_hook_terbaik[:1200])}</pre>\n\n"
        f"<b>Pantangan QC Baru:</b> <code>{html.escape(pantangan_str[:400])}</code>\n\n"
        f"📊 <a href=\"{html.escape(gs_link)}\">Buka Google Sheets (Tab Audit_Riset_Threads)</a>"
    )
    tg_status, tg_detail = send_telegram_message(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        html_text=tg_html,
        http_post=http_post,
    )
    return {
        "sources": sumber_str,
        "model": model_label,
        "pantangan_count": len(report.pantangan_qc),
        "telegram_status": tg_status,
        "telegram_detail": tg_detail,
    }

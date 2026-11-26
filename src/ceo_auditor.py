"""Agent 7 — Super Audit (Asisten CEO).

Mengaudit keseluruhan kinerja operasional Kantor Affiliate setiap minggu
(Minggu 20.30 WIB) atau kapan saja dipicu manual:
1. Kesehatan antrean & distribusi kategori produk (`Antrean_Produk`).
2. Kinerja publikasi, QC, Telegram, dan rasio fallback/kuota AI (`Log_Performa`).
3. Performa engagement aktual akun `@bilditmoy` di Meta Threads + komentar (`Komentar_Threads`).
4. Kesiapan riset hook Agent 6 (`Audit_Riset_Threads`) & umur token Threads.
5. Menyusun Skor Kesehatan Kantor (0-100), evaluasi konten, dan 3-5 aksi prioritas
   CEO ke tab `Laporan_Super_Audit_CEO` + laporan eksekutif ke Telegram.
"""
from __future__ import annotations

import html
from collections import Counter
from typing import Any, Literal

import requests
from pydantic import BaseModel, Field

from src.config import Settings, now_wib_str, sheet_url
from src.engagement_manager import check_token_age_warning
from src.gemini_client import GEMINI_NEW_KEY_TUTORIAL, GeminiClient
from src.publishers import send_telegram_message
from src.sheets_client import SheetsClient
from src.trend_researcher import fetch_own_threads_performance


class SuperAuditReport(BaseModel):
    skor_kesehatan_kantor: int = Field(
        ge=0, le=100, description="Skor kesehatan sistem 0-100 berdasarkan data riil"
    )
    status_kantor: Literal["SEHAT", "PERLU_PERHATIAN", "KRITIS"] = Field(
        description="Status menyeluruh operasional kantor"
    )
    ringkasan_eksekutif_ceo: str = Field(
        description="Ringkasan eksekutif 3-5 kalimat untuk CEO tentang kondisi minggu ini"
    )
    evaluasi_konten_dan_hook: str = Field(
        description="Evaluasi kategori produk, gaya tulisan, dan performa engagement Threads"
    )
    evaluasi_model_ai_dan_kuota: str = Field(
        description="Analisis pemakaian Gemini vs fallback/OpenRouter serta status kuota API"
    )
    rekomendasi_aksi_ceo: str = Field(
        description="3-5 langkah strategis konkret untuk CEO minggu depan (bernomor)"
    )
    rekomendasi_prompt_agent2: str = Field(
        description="Saran perbaikan spesifik untuk Copywriter/Pengaturan_Agent agar CTR & reply naik"
    )


def compute_operational_metrics(
    *,
    sheets: SheetsClient,
    sheet_config: dict[str, str],
    own_posts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Hitung metrik kuantitatif operasional dari seluruh tab Google Sheets & Threads."""
    queue = sheets.list_queue_rows()
    logs = sheets.list_performance_logs(limit=60)
    comments = sheets.list_comment_rows(limit=60)
    latest_research = sheets.get_latest_active_audit()

    status_counts = Counter(r.status for r in queue if r.status != "CONTOH")
    category_counts = Counter(r.kategori for r in queue if r.status == "PENDING" and r.kategori)

    live_published = sum(1 for l in logs if l.get("Status_Threads") == "PUBLISHED")
    draft_only_logs = sum(1 for l in logs if l.get("Status_Threads") == "SKIPPED_NO_TOKEN")
    tg_sent = sum(1 for l in logs if l.get("Status_Telegram") == "SENT")
    fallback_runs = sum(
        1 for l in logs if "fallback" in l.get("Model_AI_Dipakai", "").lower()
    )
    openrouter_runs = sum(
        1 for l in logs if "openrouter/" in l.get("Model_AI_Dipakai", "").lower()
    )

    comment_status_counts = Counter(c.get("Status_Balasan", "") for c in comments)
    token_warning = check_token_age_warning(sheet_config.get("Tanggal_Token_Threads", ""))

    total_views = sum(p.get("metrics", {}).get("views", 0) for p in own_posts)
    total_likes = sum(p.get("metrics", {}).get("likes", 0) for p in own_posts)
    total_replies = sum(p.get("metrics", {}).get("replies", 0) for p in own_posts)

    return {
        "antrean": dict(status_counts),
        "kategori_pending_terbanyak": category_counts.most_common(5),
        "log_terakhir_total": len(logs),
        "log_threads_published": live_published,
        "log_draft_only": draft_only_logs,
        "log_telegram_sent": tg_sent,
        "log_model_fallback": fallback_runs,
        "log_openrouter_fallback": openrouter_runs,
        "komentar_total": len(comments),
        "komentar_per_status": dict(comment_status_counts),
        "threads_recent_count": len(own_posts),
        "threads_total_views": total_views,
        "threads_total_likes": total_likes,
        "threads_total_replies": total_replies,
        "audit_riset_agent6_aktif": bool(latest_research),
        "tanggal_audit_agent6_terakhir": (latest_research or {}).get("Tanggal_Audit_WIB", "-"),
        "token_warning": token_warning or "Aman",
        "auto_reply_komentar": sheet_config.get("Auto_Reply_Komentar", "OFF"),
    }


def run_super_audit(
    *,
    settings: Settings,
    sheets: SheetsClient,
    gemini: GeminiClient,
    http_get: Any = requests.get,
    http_post: Any = requests.post,
) -> dict[str, Any]:
    """Jalankan Agent 7 — Super Audit (Asisten CEO) dan kirim laporan eksekutif ke Telegram."""
    sheet_config = sheets.get_settings()
    gs_link = sheet_url(settings.google_sheet_id)

    own_posts = fetch_own_threads_performance(
        user_id=settings.threads_user_id,
        access_token=settings.threads_access_token,
        limit=12,
        http_get=http_get,
    )
    metrics = compute_operational_metrics(
        sheets=sheets, sheet_config=sheet_config, own_posts=own_posts
    )
    recent_logs = sheets.list_performance_logs(limit=10)
    log_samples = [
        {
            "tanggal": l.get("Tanggal_WIB", ""),
            "produk": l.get("Nama_Produk", "")[:60],
            "status_threads": l.get("Status_Threads", ""),
            "status_tg": l.get("Status_Telegram", ""),
            "model": l.get("Model_AI_Dipakai", ""),
            "draft_threads_awal": l.get("Draft_Threads", "")[:140],
        }
        for l in recent_logs
    ]

    system_prompt = (
        "Kamu adalah Asisten Eksekutif CEO & Kepala Auditor Sistem (Agent 7 — Super Audit) "
        "untuk Kantor Agent Affiliate Shopee (Meta Threads + X).\n"
        "Tugasmu menilai kesehatan sistem secara objektif dari data metrik, "
        "mengevaluasi kualitas konten/hook dan keandalan kuota AI (Gemini 3.x + OpenRouter), "
        "serta memberi rekomendasi strategis yang ringkas, tegas, dan langsung bisa dieksekusi CEO.\n"
        "Catatan penting: Jangan menyarankan model lawas seperti gemini-1.5-pro; fokus pada "
        "evaluasi efektivitas posting organik saat ini dan putuskan apakah fitur tambahan "
        "'Agent 8 Semi-Auto Social Listening ke Telegram' (mencari postingan orang yang butuh barang "
        "untuk dibalas manual lewat Telegram) BENAR-BENAR diperlukan minggu depan berdasarkan data views & replies."
    )
    user_prompt = (
        f"Data Metrik Operasional Kantor:\n{metrics}\n\n"
        f"Sampel 10 Log Publikasi Terbaru:\n{log_samples}\n\n"
        f"Sampel Postingan Threads & Metrik Terkini:\n{own_posts[:6]}\n\n"
        "Susun Laporan Super Audit untuk CEO. Pastikan di `rekomendasi_aksi_ceo` ada 1 poin tegas "
        "mengenai perlu atau tidaknya menambah Agent 8 (Semi-Auto Social Listening ke Telegram) "
        "berdasarkan performa views, likes, dan komentar Threads sejauh ini."
    )

    try:
        report, meta = gemini.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_model=SuperAuditReport,
            temperature=0.35,
        )
    except Exception as exc:
        send_telegram_message(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            html_text=(
                f"❌ <b>Agent 7 (Super Audit CEO) Gagal:</b>\n"
                f"<code>{html.escape(str(exc)[:450])}</code>\n\n"
                f"<pre>{html.escape(GEMINI_NEW_KEY_TUTORIAL)}</pre>\n\n"
                f"📊 <a href=\"{html.escape(gs_link)}\">Buka Google Sheets</a>"
            ),
            http_post=http_post,
        )
        raise

    now_str = now_wib_str()
    periode = f"Audit per {now_str[:10]}"
    model_label = meta.display_label()

    sheets.append_ceo_audit_report(
        {
            "Tanggal_Audit_WIB": now_str,
            "Periode_Audit": periode,
            "Skor_Kesehatan_Kantor": str(report.skor_kesehatan_kantor),
            "Status_Kantor": report.status_kantor,
            "Ringkasan_Eksekutif_CEO": report.ringkasan_eksekutif_ceo,
            "Evaluasi_Konten_&_Hook": report.evaluasi_konten_dan_hook,
            "Evaluasi_Model_AI_&_Kuota": report.evaluasi_model_ai_dan_kuota,
            "Rekomendasi_Aksi_CEO": report.rekomendasi_aksi_ceo,
            "Rekomendasi_Prompt_Agent2": report.rekomendasi_prompt_agent2,
            "Model_AI_Dipakai": model_label,
        }
    )

    q_warn = meta.quota_warning_message()
    quota_block = (
        f"\n⚠️ <b>PERINGATAN KUOTA GEMINI:</b>\n{html.escape(q_warn)}\n"
        if q_warn
        else ""
    )
    status_emoji = {"SEHAT": "🟢", "PERLU_PERHATIAN": "🟡", "KRITIS": "🔴"}.get(
        report.status_kantor, "⚪"
    )

    tg_html = (
        f"👔 <b>LAPORAN SUPER AUDIT — ASISTEN CEO (Agent 7)</b>\n"
        f"<b>Skor Kesehatan:</b> {status_emoji} <b>{report.skor_kesehatan_kantor}/100 ({report.status_kantor})</b>\n"
        f"<b>Model AI Audit:</b> <code>{html.escape(model_label)}</code>\n"
        f"<b>Ringkas Angka:</b> PENDING={metrics['antrean'].get('PENDING', 0)} | "
        f"PUBLISHED={metrics['log_threads_published']} | Views={metrics['threads_total_views']} | "
        f"Replies={metrics['threads_total_replies']}\n"
        f"{quota_block}\n"
        f"<b>Ringkasan Eksekutif:</b>\n{html.escape(report.ringkasan_eksekutif_ceo)}\n\n"
        f"<b>Evaluasi Konten &amp; Kuota AI:</b>\n"
        f"• {html.escape(report.evaluasi_konten_dan_hook[:500])}\n"
        f"• {html.escape(report.evaluasi_model_ai_dan_kuota[:400])}\n\n"
        f"<b>Aksi Prioritas CEO Minggu Ini:</b>\n"
        f"<pre>{html.escape(report.rekomendasi_aksi_ceo[:900])}</pre>\n\n"
        f"📊 <a href=\"{html.escape(gs_link)}\">Buka Google Sheets (Tab Laporan_Super_Audit_CEO)</a>"
    )
    tg_status, tg_detail = send_telegram_message(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        html_text=tg_html,
        http_post=http_post,
    )
    return {
        "score": report.skor_kesehatan_kantor,
        "status": report.status_kantor,
        "model": model_label,
        "telegram_status": tg_status,
        "telegram_detail": tg_detail,
    }

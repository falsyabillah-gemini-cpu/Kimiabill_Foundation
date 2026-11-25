"""Agent 4 & 5 — Admin Komentar Threads & Office Manager (Jalan jam 21.00 WIB).

Tugas:
1. Mengecek komentar baru pada postingan Threads yang sudah tayang.
2. Membuat saran balasan ramah menggunakan Gemini (dengan fallback otomatis).
3. Mencatat ke tab `Komentar_Threads` (dan membalas otomatis jika `Auto_Reply_Komentar = ON`).
4. Mengecek umur token Threads (`Tanggal_Token_Threads`) dan stok antrean `PENDING`.
5. Mengirim ringkasan laporan malam ke Telegram.
"""
from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any

import requests
from pydantic import BaseModel, Field

from src.config import Settings, now_wib, now_wib_str, sheet_url
from src.gemini_client import GeminiClient
from src.publishers import THREADS_API_BASE, _publish_single_threads_post, send_telegram_message
from src.sheets_client import SheetsClient


class CommentReplySuggestion(BaseModel):
    reply_text: str = Field(
        description="Balasan ramah, natural, membantu (maks 280 karakter), tanpa spam."
    )


def check_token_age_warning(
    token_created_str: str, *, today: date | None = None
) -> str | None:
    """Ingatkan user jika token Threads sudah berumur >= 50 hari (limit Meta ~60 hari)."""
    raw = (token_created_str or "").strip()
    if not raw:
        return None
    try:
        created = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return (
            f"⚠️ Format `Tanggal_Token_Threads` ('{raw}') tidak valid. "
            "Gunakan format YYYY-MM-DD di tab Pengaturan_Agent."
        )
    ref_today = today or now_wib().date()
    age_days = (ref_today - created).days
    if age_days >= 50:
        return (
            f"🔑 <b>Peringatan Token Threads:</b> Token Anda sudah berumur <b>{age_days} hari</b> "
            "(dibuat {raw}). Segera perpanjang Long-Lived Token sebelum kedaluwarsa di hari ke-60!"
        ).format(raw=raw)
    return None


def fetch_threads_replies(
    *,
    post_id: str,
    access_token: str,
    http_get: Any = requests.get,
) -> tuple[list[dict[str, Any]], str | None]:
    """Ambil komentar pada 1 postingan Threads. Kembalikan `(replies, error_msg)`."""
    if not post_id or not access_token:
        return [], None
    try:
        resp = http_get(
            f"{THREADS_API_BASE}/{post_id}/replies",
            params={
                "fields": "id,text,username,timestamp,hide_status",
                "access_token": access_token,
            },
            timeout=25,
        )
        if resp.status_code == 401 or (
            resp.status_code >= 400 and resp.json().get("error", {}).get("code") == 190
        ):
            return [], "Token Threads kedaluwarsa/tidak valid (HTTP 401/190)."
        if resp.status_code >= 400:
            return [], f"Threads API HTTP {resp.status_code}: {resp.text[:160]}"
        return resp.json().get("data", []) or [], None
    except Exception as exc:
        return [], str(exc)


def run_engagement_manager(
    *,
    settings: Settings,
    sheets: SheetsClient,
    gemini: GeminiClient,
    http_get: Any = requests.get,
    http_post: Any = requests.post,
) -> dict[str, Any]:
    """Jalankan pemeriksaan malam (komentar Threads, umur token, dan stok PENDING)."""
    sheet_config = sheets.get_settings()
    pending_count = sheets.count_pending_products()
    try:
        min_stock = int(sheet_config.get("Min_Stok_Peringatan", "5") or 5)
    except ValueError:
        min_stock = 5

    auto_reply_on = sheet_config.get("Auto_Reply_Komentar", "OFF").strip().upper() == "ON"
    persona = sheet_config.get("Persona_Panggilan", "aku / kamu")
    gaya = sheet_config.get(
        "Gaya_Bahasa_Threads",
        "Santai, jujur, conversational, ramah seperti teman",
    )

    alerts: list[str] = []
    if pending_count < min_stock:
        alerts.append(
            f"📦 <b>Stok Produk Menipis:</b> Antrean <code>PENDING</code> tinggal "
            f"<b>{pending_count}</b> (batas minimum: {min_stock}). Tambahkan produk di tab <i>Antrean_Produk</i>."
        )

    token_warning = check_token_age_warning(sheet_config.get("Tanggal_Token_Threads", ""))
    if token_warning:
        alerts.append(token_warning)

    new_suggestions: list[dict[str, str]] = []
    token_error_msg: str | None = None

    if settings.threads_access_token and settings.threads_user_id:
        existing_ids = sheets.list_existing_comment_ids()
        recent_posts = sheets.list_published_post_ids(limit=8)

        for post in recent_posts:
            post_id = post.get("Threads_Post_ID", "")
            product_name = post.get("Nama_Produk", "")
            own_reply_id = post.get("Threads_Reply_ID", "")
            replies, err = fetch_threads_replies(
                post_id=post_id,
                access_token=settings.threads_access_token,
                http_get=http_get,
            )
            if err:
                token_error_msg = err
                break

            for rep in replies:
                cid = str(rep.get("id", "")).strip()
                text = str(rep.get("text", "")).strip()
                username = str(rep.get("username", "")).strip() or "pengguna"
                if not cid or not text:
                    continue
                if cid == own_reply_id or cid in existing_ids:
                    continue
                # Hindari membalas komentar yang sebenarnya balasan link kita sendiri
                if "shopee.co.id" in text.lower() or "s.shopee" in text.lower():
                    continue

                suggestion_obj, meta = gemini.generate_structured(
                    system_prompt=(
                        "Kamu Admin Komentar Meta Threads yang ramah dan membantu.\n"
                        f"Gaya bahasa: {gaya}\nSapaan: {persona}\n"
                        "Tugas: Balas komentar audiens secara singkat, natural, tidak kaku, "
                        "dan arahkan cek komentar link produk jika mereka menanyakan link/harga."
                    ),
                    user_prompt=(
                        f"Produk yang dibahas: {product_name}\n"
                        f"Komentar dari @{username}: {text}"
                    ),
                    response_model=CommentReplySuggestion,
                    temperature=0.7,
                )
                reply_text = suggestion_obj.reply_text.strip()
                status_balasan = "MENUNGGU_BALASAN"

                if auto_reply_on and not settings.dry_run:
                    try:
                        _publish_single_threads_post(
                            user_id=settings.threads_user_id,
                            access_token=settings.threads_access_token,
                            text=reply_text,
                            reply_to_id=cid,
                            http_post=http_post,
                            http_get=http_get,
                        )
                        status_balasan = "SUDAH_DIBALAS"
                    except Exception as exc:
                        alerts.append(
                            f"⚠️ Gagal auto-reply komentar @{html.escape(username)}: {html.escape(str(exc))}"
                        )

                sheets.append_comment_row(
                    {
                        "Tanggal_WIB": now_wib_str(),
                        "Threads_Post_ID": post_id,
                        "Comment_ID": cid,
                        "Username_Komentar": f"@{username.lstrip('@')}",
                        "Isi_Komentar": text,
                        "Saran_Balasan_AI": reply_text,
                        "Model_AI_Dipakai": meta.display_label(),
                        "Status_Balasan": status_balasan,
                        "Balasan_Manual_User": "",
                    }
                )
                existing_ids.add(cid)
                new_suggestions.append(
                    {
                        "username": f"@{username.lstrip('@')}",
                        "comment": text,
                        "reply": reply_text,
                        "status": status_balasan,
                        "model": meta.display_label(),
                    }
                )

    if token_error_msg:
        alerts.append(f"🔑 <b>Masalah Token Threads:</b> {html.escape(token_error_msg)}")

    mode_badge = "🧪 DRY RUN" if settings.dry_run else "🌙 LAPORAN MALAM (21.00 WIB)"
    lines = [
        f"<b>{mode_badge} — Office Manager</b>",
        f"Waktu: {html.escape(now_wib_str())}",
        f"Stok antrean PENDING: <b>{pending_count}</b>",
        f"Komentar baru ditemukan: <b>{len(new_suggestions)}</b>",
    ]
    if alerts:
        lines.append("\n<b>Peringatan:</b>")
        lines.extend(alerts)
    if new_suggestions:
        lines.append("\n<b>Saran Balasan Komentar:</b>")
        for item in new_suggestions[:8]:
            lines.append(
                f"• <b>{html.escape(item['username'])}</b>: \"{html.escape(item['comment'])}\"\n"
                f"  ↳ <i>[{html.escape(item['status'])} | {html.escape(item['model'])}]</i>\n"
                f"  <pre>{html.escape(item['reply'])}</pre>"
            )

    if settings.google_sheet_id:
        gs_link = sheet_url(settings.google_sheet_id)
        lines.append(
            f"\n📊 <a href=\"{html.escape(gs_link)}\">Buka Google Sheets (Antrean &amp; Komentar)</a>"
        )

    tg_status, tg_detail = send_telegram_message(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        html_text="\n".join(lines),
        http_post=http_post,
    )
    return {
        "pending_count": pending_count,
        "new_comments": len(new_suggestions),
        "alerts": alerts,
        "telegram_status": tg_status,
        "telegram_detail": tg_detail,
    }

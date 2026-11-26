"""Agent 3 — Publisher & Dispatcher (Meta Threads + Telegram)."""
from __future__ import annotations

import html
import random
import time
from dataclasses import dataclass
from typing import Any

import requests

from src.config import (
    AFFILIATE_CONTENT_TYPE,
    is_organic_content_type,
    normalize_content_type,
)

THREADS_API_BASE = "https://graph.threads.net/v1.0"
TELEGRAM_API_BASE = "https://api.telegram.org"


@dataclass(frozen=True)
class ThreadsPublishResult:
    status: str  # PUBLISHED | DRY_RUN | SKIPPED_NO_TOKEN | NEED_PHOTO | ERROR
    post_id: str = ""
    reply_id: str = ""
    permalink: str = ""
    error_message: str = ""


class ThreadsTokenError(RuntimeError):
    """Raised when Threads access token is expired or unauthorized (code 190 / HTTP 401)."""


def _is_https_url(url: str) -> bool:
    return (url or "").strip().lower().startswith("https://")


def verify_public_image_url(
    image_url: str,
    *,
    http_get: Any = requests.get,
) -> tuple[bool, str]:
    """Pastikan `URL_Foto_Publik` valid HTTPS dan mengembalikan HTTP 200."""
    cleaned = (image_url or "").strip()
    if not cleaned:
        return False, "URL_Foto_Publik kosong"
    if not _is_https_url(cleaned):
        return False, "URL_Foto_Publik wajib memakai skema https://"

    try:
        try:
            resp = http_get(cleaned, timeout=15, stream=True, allow_redirects=True)
        except TypeError:
            resp = http_get(cleaned, timeout=15)
        status_code = getattr(resp, "status_code", None)
        if status_code != 200:
            return False, f"URL_Foto_Publik gagal diakses (HTTP {status_code})"
        headers = getattr(resp, "headers", None) or {}
        if isinstance(headers, dict):
            content_type = str(
                headers.get("Content-Type") or headers.get("content-type") or ""
            ).lower()
            if content_type and not (
                content_type.startswith("image/")
                or "octet-stream" in content_type
            ):
                return False, f"URL_Foto_Publik bukan gambar ({content_type[:60]})"
        close_fn = getattr(resp, "close", None)
        if callable(close_fn):
            close_fn()
        return True, "HTTP 200 OK"
    except Exception as exc:
        return False, f"URL_Foto_Publik error saat verifikasi: {str(exc)[:120]}"


def _wait_container_ready(
    creation_id: str,
    access_token: str,
    *,
    http_get: Any = requests.get,
    sleep_fn: Any = time.sleep,
    max_checks: int = 5,
) -> None:
    url = f"{THREADS_API_BASE}/{creation_id}"
    for _ in range(max_checks):
        try:
            resp = http_get(
                url,
                params={"fields": "status,error_message", "access_token": access_token},
                timeout=20,
            )
            if resp.status_code != 200:
                return
            data = resp.json()
            status = str(data.get("status", "")).upper()
            if status in ("FINISHED", "PUBLISHED", ""):
                return
            if status == "ERROR":
                raise RuntimeError(
                    f"Threads media container error: {data.get('error_message', 'unknown')}"
                )
        except RuntimeError:
            raise
        except Exception:
            return
        sleep_fn(2)


def _check_threads_error(resp: requests.Response, step: str) -> None:
    if resp.status_code < 400:
        return
    try:
        err = resp.json().get("error", {})
        code = err.get("code")
        msg = err.get("message", resp.text[:200])
    except Exception:
        code = None
        msg = resp.text[:200]

    if resp.status_code == 401 or code == 190:
        raise ThreadsTokenError(f"Token Threads kedaluwarsa/tidak valid pada {step}: {msg}")
    raise RuntimeError(f"Threads API HTTP {resp.status_code} pada {step}: {msg}")


def _publish_single_threads_post(
    *,
    user_id: str,
    access_token: str,
    text: str,
    image_url: str = "",
    reply_to_id: str = "",
    http_post: Any = requests.post,
    http_get: Any = requests.get,
    sleep_fn: Any = time.sleep,
) -> str:
    payload: dict[str, str] = {
        "text": text,
        "access_token": access_token,
    }
    if reply_to_id:
        payload["media_type"] = "TEXT"
        payload["reply_to_id"] = reply_to_id
    elif _is_https_url(image_url):
        payload["media_type"] = "IMAGE"
        payload["image_url"] = image_url.strip()
    else:
        payload["media_type"] = "TEXT"

    create_resp = http_post(f"{THREADS_API_BASE}/{user_id}/threads", data=payload, timeout=25)
    _check_threads_error(create_resp, "create container")
    creation_id = str(create_resp.json().get("id", "")).strip()
    if not creation_id:
        raise RuntimeError("Threads API tidak mengembalikan creation_id.")

    _wait_container_ready(
        creation_id, access_token, http_get=http_get, sleep_fn=sleep_fn
    )

    pub_resp = http_post(
        f"{THREADS_API_BASE}/{user_id}/threads_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=25,
    )
    _check_threads_error(pub_resp, "publish container")
    post_id = str(pub_resp.json().get("id", "")).strip()
    if not post_id:
        raise RuntimeError("Threads API tidak mengembalikan post_id setelah publish.")
    return post_id


def publish_to_threads(
    *,
    user_id: str,
    access_token: str,
    main_post: str,
    reply_text: str = "",
    image_url: str = "",
    tipe_konten: str = AFFILIATE_CONTENT_TYPE,
    dry_run: bool = False,
    verify_photo: bool = True,
    delay_range: tuple[int, int] = (90, 300),
    http_post: Any = requests.post,
    http_get: Any = requests.get,
    sleep_fn: Any = time.sleep,
) -> ThreadsPublishResult:
    """Publish utas Meta Threads (organik tunggal atau soft affiliate berfoto + delayed reply)."""
    normalized_type = normalize_content_type(tipe_konten)
    organic = is_organic_content_type(normalized_type)

    if not organic and verify_photo:
        photo_ok, photo_reason = verify_public_image_url(image_url, http_get=http_get)
        if not photo_ok:
            return ThreadsPublishResult(
                status="NEED_PHOTO",
                error_message=photo_reason,
            )

    if dry_run:
        return ThreadsPublishResult(status="DRY_RUN")
    if not user_id or not access_token:
        return ThreadsPublishResult(status="SKIPPED_NO_TOKEN")

    try:
        post_id = _publish_single_threads_post(
            user_id=user_id,
            access_token=access_token,
            text=main_post,
            image_url=image_url if not organic else "",
            http_post=http_post,
            http_get=http_get,
            sleep_fn=sleep_fn,
        )
        permalink = ""
        try:
            meta_resp = http_get(
                f"{THREADS_API_BASE}/{post_id}",
                params={"fields": "id,permalink", "access_token": access_token},
                timeout=20,
            )
            if meta_resp.status_code == 200:
                permalink = str(meta_resp.json().get("permalink", "")).strip()
        except Exception:
            pass

        reply_id = ""
        clean_reply = (reply_text or "").strip()
        if not organic and clean_reply:
            low_d, high_d = delay_range
            if high_d > 0:
                delay_sec = random.randint(max(0, low_d), max(low_d, high_d))
                if delay_sec > 0:
                    sleep_fn(delay_sec)
            reply_id = _publish_single_threads_post(
                user_id=user_id,
                access_token=access_token,
                text=clean_reply,
                reply_to_id=post_id,
                http_post=http_post,
                http_get=http_get,
                sleep_fn=sleep_fn,
            )

        return ThreadsPublishResult(
            status="PUBLISHED",
            post_id=post_id,
            reply_id=reply_id,
            permalink=permalink,
        )
    except Exception as exc:
        return ThreadsPublishResult(status="ERROR", error_message=str(exc))


def send_telegram_message(
    *,
    bot_token: str,
    chat_id: str,
    html_text: str,
    http_post: Any = requests.post,
) -> tuple[str, str]:
    """Kirim pesan HTML ke Telegram Bot API. Mengembalikan `(status, detail)`."""
    if not bot_token or not chat_id:
        return "SKIPPED", "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum diisi"

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    try:
        resp = http_post(
            url,
            json={
                "chat_id": chat_id,
                "text": html_text[:4090],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=25,
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            return "SENT", ""
        return "FAILED", f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return "FAILED", str(exc)


def format_need_photo_telegram_message(
    *,
    product_name: str,
    kategori: str,
    row_number: int,
    reason: str,
    sheet_link: str = "",
) -> str:
    """Bangun notifikasi Telegram saat produk `SOFT_AFFILIATE` gagal lolos gatekeeping foto."""
    sheet_footer = (
        f"\n\n📊 <a href=\"{html.escape(sheet_link)}\">Isi URL_Foto_Publik di Google Sheets</a>"
        if sheet_link
        else ""
    )
    return (
        "📸 <b>NEED_PHOTO — Publikasi Ditunda</b>\n"
        f"<b>Produk:</b> {html.escape(product_name)} ({html.escape(kategori)})\n"
        f"<b>Baris Antrean:</b> #{row_number}\n"
        f"<b>Alasan:</b> <code>{html.escape(reason)}</code>\n\n"
        "Produk tidak diposting tanpa foto agar akun Threads tidak terbaca bot teks polos.\n"
        "Silakan isi kolom <code>URL_Foto_Publik</code> (link gambar HTTPS valid HTTP 200) "
        "lalu ubah statusnya kembali ke <code>PENDING</code>."
        f"{sheet_footer}"
    )


def format_publish_telegram_message(
    *,
    product_name: str,
    kategori: str,
    threads_result: ThreadsPublishResult,
    threads_main_post: str,
    threads_reply_text: str,
    x_draft_post: str,
    model_label: str,
    qc_notes: str,
    pending_remaining: int,
    min_stock: int,
    dry_run: bool,
    tipe_konten: str = AFFILIATE_CONTENT_TYPE,
    sheet_link: str = "",
    quota_warning: str | None = None,
) -> str:
    """Bangun pesan Telegram rapi berisi status Threads + draf X siap tap-to-copy."""
    mode_badge = "🧪 DRY RUN" if dry_run else "🚀 LIVE RUN"
    normalized_type = normalize_content_type(tipe_konten)
    header = (
        f"<b>{mode_badge} — Kantor Agent Affiliate</b>\n"
        f"<b>Tipe Konten:</b> <code>{html.escape(normalized_type)}</code>\n"
        f"<b>Topik/Produk:</b> {html.escape(product_name)} ({html.escape(kategori)})\n"
        f"<b>Model AI:</b> <code>{html.escape(model_label)}</code>\n"
        f"<b>Status Threads:</b> {html.escape(threads_result.status)}"
    )
    if threads_result.permalink:
        header += f"\n<b>Link Threads:</b> {html.escape(threads_result.permalink)}"
    elif threads_result.error_message:
        header += f"\n<b>Info Threads:</b> {html.escape(threads_result.error_message)}"

    stock_line = f"📦 Sisa antrean PENDING: <b>{pending_remaining}</b>"
    if pending_remaining < min_stock:
        stock_line += f" ⚠️ <i>(Di bawah batas minimum {min_stock}!)</i>"

    quota_block = (
        f"\n⚠️ <b>PERINGATAN KUOTA GEMINI:</b> {html.escape(quota_warning)}"
        if quota_warning
        else ""
    )

    sheet_footer = (
        f"\n\n📊 <a href=\"{html.escape(sheet_link)}\">Buka Google Sheets (Antrean &amp; Laporan)</a>"
        if sheet_link
        else ""
    )

    reply_block = (
        f"\n\n<b>💬 Balasan Link Threads:</b>\n<pre>{html.escape(threads_reply_text)}</pre>"
        if threads_reply_text.strip()
        else "\n\n<b>💬 Balasan Link Threads:</b> <i>Tidak ada (konten organik tanpa link)</i>"
    )
    x_block = (
        f"\n\n<b>𝕏 Draf X (Siap Copy-Paste):</b>\n<pre>{html.escape(x_draft_post)}</pre>"
        if x_draft_post.strip()
        else ""
    )

    return (
        f"{header}\n"
        f"{stock_line}"
        f"{quota_block}\n"
        f"<b>QC:</b> {html.escape(qc_notes)}\n\n"
        f"<b>🧵 Draf Threads (Utas Utama):</b>\n"
        f"<pre>{html.escape(threads_main_post)}</pre>"
        f"{reply_block}"
        f"{x_block}"
        f"{sheet_footer}"
    )

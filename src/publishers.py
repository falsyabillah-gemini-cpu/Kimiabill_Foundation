"""Agent 3 — Publisher & Dispatcher (Meta Threads + Telegram)."""
from __future__ import annotations

import html
import time
from dataclasses import dataclass
from typing import Any

import requests

THREADS_API_BASE = "https://graph.threads.net/v1.0"
TELEGRAM_API_BASE = "https://api.telegram.org"


@dataclass(frozen=True)
class ThreadsPublishResult:
    status: str  # PUBLISHED | DRY_RUN | SKIPPED_NO_TOKEN | ERROR
    post_id: str = ""
    reply_id: str = ""
    permalink: str = ""
    error_message: str = ""


class ThreadsTokenError(RuntimeError):
    """Raised when Threads access token is expired or unauthorized (code 190 / HTTP 401)."""


def _is_https_url(url: str) -> bool:
    return (url or "").strip().lower().startswith("https://")


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
    reply_text: str,
    image_url: str = "",
    dry_run: bool = False,
    http_post: Any = requests.post,
    http_get: Any = requests.get,
    sleep_fn: Any = time.sleep,
) -> ThreadsPublishResult:
    """Publish utas utama (+ gambar opsional) ke Meta Threads dan auto-reply link."""
    if dry_run:
        return ThreadsPublishResult(status="DRY_RUN")
    if not user_id or not access_token:
        return ThreadsPublishResult(status="SKIPPED_NO_TOKEN")

    try:
        post_id = _publish_single_threads_post(
            user_id=user_id,
            access_token=access_token,
            text=main_post,
            image_url=image_url,
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

        # Beri jeda singkat sebelum auto-reply link di bawah utas utama
        sleep_fn(2)
        reply_id = _publish_single_threads_post(
            user_id=user_id,
            access_token=access_token,
            text=reply_text,
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
    sheet_link: str = "",
    quota_warning: str | None = None,
) -> str:
    """Bangun pesan Telegram rapi berisi status Threads + draf X siap tap-to-copy."""
    mode_badge = "🧪 DRY RUN" if dry_run else "🚀 LIVE RUN"
    header = (
        f"<b>{mode_badge} — Kantor Agent Affiliate</b>\n"
        f"<b>Produk:</b> {html.escape(product_name)} ({html.escape(kategori)})\n"
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

    return (
        f"{header}\n"
        f"{stock_line}"
        f"{quota_block}\n"
        f"<b>QC:</b> {html.escape(qc_notes)}\n\n"
        f"<b>🧵 Draf Threads (Utas Utama):</b>\n"
        f"<pre>{html.escape(threads_main_post)}</pre>\n\n"
        f"<b>💬 Balasan Link Threads:</b>\n"
        f"<pre>{html.escape(threads_reply_text)}</pre>\n\n"
        f"<b>𝕏 Draf X (Siap Copy-Paste):</b>\n"
        f"<pre>{html.escape(x_draft_post)}</pre>"
        f"{sheet_footer}"
    )

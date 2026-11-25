"""Entrypoint CLI untuk Kantor Agent Affiliate Online (Dual Threads + X)."""
from __future__ import annotations

import argparse
import html
import sys
from typing import Any

from pydantic import BaseModel

from src.config import (
    DEFAULT_GEMINI_PRIMARY_MODEL,
    Settings,
    now_wib_str,
    parse_model_chain,
    sheet_url,
)
from src.copywriter_agent import generate_copywriting
from src.csv_importer import import_csv_to_sheets
from src.engagement_manager import run_engagement_manager
from src.gemini_client import GeminiClient
from src.publishers import (
    format_publish_telegram_message,
    publish_to_threads,
    send_telegram_message,
)
from src.sheets_client import SheetsClient
from src.shopee_hunter import pick_next_product
from src.trend_researcher import run_trend_researcher


def _build_gemini_client(settings: Settings, sheet_config: dict[str, str]) -> GeminiClient:
    primary = (
        settings.gemini_primary_model
        if settings.gemini_primary_model != DEFAULT_GEMINI_PRIMARY_MODEL
        else (sheet_config.get("Model_Gemini_Utama", "").strip() or DEFAULT_GEMINI_PRIMARY_MODEL)
    )
    fallback_chain = parse_model_chain(
        sheet_config.get("Rantai_Fallback_Gemini", ""),
        primary=primary,
    )
    return GeminiClient(
        settings.gemini_api_key,
        primary_model=primary,
        fallback_chain=fallback_chain,
    )


def run_publish_pipeline(
    settings: Settings,
    *,
    sheets: SheetsClient | None = None,
    gemini: GeminiClient | None = None,
) -> int:
    """Jalankan 1 siklus Agent 1 -> Agent 2 -> Agent 3."""
    sheets_client = sheets or SheetsClient(
        settings.google_sheet_id, settings.google_service_account_json
    )
    sheet_config = sheets_client.get_settings()
    gemini_client = gemini or _build_gemini_client(settings, sheet_config)
    gs_link = sheet_url(settings.google_sheet_id)

    try:
        min_stock = int(sheet_config.get("Min_Stok_Peringatan", "5") or 5)
    except ValueError:
        min_stock = 5

    product = pick_next_product(sheets_client, settings, sheet_config)
    if product is None:
        send_telegram_message(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            html_text=(
                "📦 <b>Kantor Agent Affiliate:</b> Tidak ada produk berstatus <code>PENDING</code> "
                "di tab <i>Antrean_Produk</i> saat jadwal berjalan.\n"
                "Tambahkan produk baru (atau isi <code>SHOPEE_APP_ID</code> &amp; <code>SHOPEE_SECRET</code>).\n\n"
                f"📊 <a href=\"{html.escape(gs_link)}\">Buka Google Sheets (Antrean &amp; Laporan)</a>"
            ),
        )
        return 0

    audit_report = sheets_client.get_latest_active_audit()
    try:
        copy_res = generate_copywriting(
            gemini_client, product, sheet_config, audit_report=audit_report
        )
    except Exception as exc:
        err_msg = f"[{now_wib_str()}] Gagal Copywriter/Gemini: {exc}"
        if not settings.dry_run:
            sheets_client.update_queue_row_fields(
                product.row_number,
                {"Status": "ERROR", "Catatan_Agent": err_msg},
            )
        else:
            sheets_client.update_queue_row_fields(
                product.row_number,
                {"Catatan_Agent": err_msg},
            )
        send_telegram_message(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            html_text=(
                f"❌ <b>Error Copywriter</b> pada produk <b>{html.escape(product.nama_produk)}</b>:\n"
                f"<code>{html.escape(str(exc))}</code>\n\n"
                f"📊 <a href=\"{html.escape(gs_link)}\">Buka Google Sheets</a>"
            ),
        )
        raise

    threads_res = publish_to_threads(
        user_id=settings.threads_user_id,
        access_token=settings.threads_access_token,
        main_post=copy_res.draft.threads_main_post,
        reply_text=copy_res.draft.threads_reply_text,
        image_url=product.url_foto_publik,
        dry_run=settings.dry_run,
    )

    pending_remaining = sheets_client.count_pending_products()
    if settings.dry_run and product.status == "PENDING":
        # Pada dry_run status tetap PENDING; hitung sisa setelah produk ini seolah tayang = pending - 1
        pass

    model_label = copy_res.meta.display_label()
    tg_msg = format_publish_telegram_message(
        product_name=product.nama_produk,
        kategori=product.kategori,
        threads_result=threads_res,
        threads_main_post=copy_res.draft.threads_main_post,
        threads_reply_text=copy_res.draft.threads_reply_text,
        x_draft_post=copy_res.draft.x_draft_post,
        model_label=model_label,
        qc_notes=copy_res.qc_notes,
        pending_remaining=pending_remaining,
        min_stock=min_stock,
        dry_run=settings.dry_run,
        sheet_link=gs_link,
        quota_warning=copy_res.meta.quota_warning_message(),
    )
    tg_status, tg_detail = send_telegram_message(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        html_text=tg_msg,
    )

    now_str = now_wib_str()
    mode_run = "DRY_RUN" if settings.dry_run else (
        "LIVE" if threads_res.status == "PUBLISHED" else "TELEGRAM_ONLY"
    )

    if settings.dry_run:
        sheets_client.update_queue_row_fields(
            product.row_number,
            {
                "Status": "PENDING",
                "Model_AI_Dipakai": model_label,
                "Catatan_Agent": f"[{now_str}] Dry-run sukses; status tetap PENDING agar siap tayang live.",
            },
        )
    elif threads_res.status == "PUBLISHED":
        sheets_client.update_queue_row_fields(
            product.row_number,
            {
                "Status": "PUBLISHED",
                "Waktu_Tayang_WIB": now_str,
                "Threads_Post_ID": threads_res.post_id,
                "Link_Post_Threads": threads_res.permalink,
                "Model_AI_Dipakai": model_label,
                "Catatan_Agent": f"[{now_str}] Sukses tayang Threads + auto-reply & Telegram ({tg_status})",
            },
        )
    elif threads_res.status == "SKIPPED_NO_TOKEN":
        sheets_client.update_queue_row_fields(
            product.row_number,
            {
                "Status": "DRAFT_ONLY",
                "Waktu_Tayang_WIB": now_str,
                "Model_AI_Dipakai": model_label,
                "Catatan_Agent": f"[{now_str}] Draf X dikirim ke Telegram ({tg_status}); Threads dilewati (token kosong)",
            },
        )
    else:
        sheets_client.update_queue_row_fields(
            product.row_number,
            {
                "Status": "ERROR",
                "Waktu_Tayang_WIB": now_str,
                "Model_AI_Dipakai": model_label,
                "Catatan_Agent": f"[{now_str}] Threads error: {threads_res.error_message}",
            },
        )

    qc_log = copy_res.qc_notes
    if copy_res.meta.fallback_notes:
        qc_log += f" | Fallback: {copy_res.meta.fallback_notes}"
    if tg_detail:
        qc_log += f" | TG: {tg_detail}"

    sheets_client.append_performance_log(
        {
            "Tanggal_WIB": now_str,
            "ID_Produk": product.product_id,
            "Nama_Produk": product.nama_produk,
            "Mode_Run": mode_run,
            "Status_Threads": threads_res.status,
            "Threads_Post_ID": threads_res.post_id,
            "Threads_Reply_ID": threads_res.reply_id,
            "Draft_Threads": copy_res.draft.threads_main_post,
            "Draft_Reply_Link": copy_res.draft.threads_reply_text,
            "Draft_X": copy_res.draft.x_draft_post,
            "Status_Telegram": tg_status,
            "Model_AI_Dipakai": model_label,
            "Catatan_QC": qc_log,
        }
    )
    print(f"[main] Selesai memproses '{product.nama_produk}' (Threads={threads_res.status}, TG={tg_status}, Model={model_label})")
    return 0 if threads_res.status != "ERROR" else 1


def run_engagement_pipeline(settings: Settings) -> int:
    sheets_client = SheetsClient(
        settings.google_sheet_id, settings.google_service_account_json
    )
    sheet_config = sheets_client.get_settings()
    gemini_client = _build_gemini_client(settings, sheet_config)
    summary = run_engagement_manager(
        settings=settings,
        sheets=sheets_client,
        gemini=gemini_client,
    )
    print(f"[main] Engagement selesai: {summary}")
    return 0


def run_check_gemini(settings: Settings) -> int:
    class Ping(BaseModel):
        status: str
        model_note: str

    gemini = GeminiClient(settings.gemini_api_key, primary_model=settings.gemini_primary_model)
    res, meta = gemini.generate_structured(
        system_prompt="Balas JSON singkat status='ok'.",
        user_prompt="Uji koneksi model Gemini.",
        response_model=Ping,
        temperature=0.0,
    )
    print(f"OK -> model={meta.display_label()}, payload={res.model_dump()}")
    return 0


def run_import_csv(settings: Settings, csv_file: str) -> int:
    sheets_client = SheetsClient(
        settings.google_sheet_id, settings.google_service_account_json
    )
    summary = import_csv_to_sheets(sheets_client, csv_file)
    print(f"[main] Impor CSV selesai: {summary}")
    return 0


def run_research_pipeline(settings: Settings) -> int:
    sheets_client = SheetsClient(
        settings.google_sheet_id, settings.google_service_account_json
    )
    sheet_config = sheets_client.get_settings()
    gemini_client = _build_gemini_client(settings, sheet_config)
    summary = run_trend_researcher(
        settings=settings,
        sheets=sheets_client,
        gemini=gemini_client,
    )
    print(f"[main] Riset Agent 6 selesai: {summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kantor Agent Affiliate Online (Dual Threads + X)")
    parser.add_argument(
        "command",
        choices=["publish", "engagement", "research", "check-gemini", "import-csv"],
        help="Mode eksekusi agent",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Aktifkan mode uji coba (override DRY_RUN env)",
    )
    parser.add_argument(
        "--file",
        dest="csv_file",
        default="",
        help="Path file CSV Shopee untuk command import-csv",
    )
    args = parser.parse_args(argv)
    settings = Settings.from_env(dry_run_override=True if args.dry_run else None)

    if args.command == "publish":
        return run_publish_pipeline(settings)
    if args.command == "engagement":
        return run_engagement_pipeline(settings)
    if args.command == "research":
        return run_research_pipeline(settings)
    if args.command == "import-csv":
        if not args.csv_file:
            parser.error("--file wajib diisi untuk command import-csv")
        return run_import_csv(settings, args.csv_file)
    return run_check_gemini(settings)


if __name__ == "__main__":
    sys.exit(main())

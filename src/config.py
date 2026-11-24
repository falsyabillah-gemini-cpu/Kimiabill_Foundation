"""Konfigurasi terpusat dan helper waktu WIB untuk Kantor Agent Affiliate."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

WIB_TZ = ZoneInfo("Asia/Jakarta")

DEFAULT_GEMINI_PRIMARY_MODEL = "gemini-4-hydrogen"
DEFAULT_GEMINI_FALLBACK_CHAIN: tuple[str, ...] = (
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
)


def now_wib() -> datetime:
    return datetime.now(tz=WIB_TZ)


def now_wib_str() -> str:
    return now_wib().strftime("%Y-%m-%d %H:%M:%S WIB")


def today_wib_str() -> str:
    return now_wib().strftime("%Y-%m-%d")


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def parse_model_chain(raw: str | None, primary: str = DEFAULT_GEMINI_PRIMARY_MODEL) -> tuple[str, ...]:
    """Parse daftar model dipisahkan koma, pastikan primary berada di urutan terdepan."""
    items: list[str] = []
    if primary and primary.strip():
        items.append(primary.strip())
    if raw:
        for part in raw.split(","):
            name = part.strip()
            if name and name not in items:
                items.append(name)
    for m in DEFAULT_GEMINI_FALLBACK_CHAIN:
        if m not in items:
            items.append(m)
    return tuple(items)


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_primary_model: str
    google_sheet_id: str
    google_service_account_json: str
    telegram_bot_token: str
    telegram_chat_id: str
    threads_user_id: str
    threads_access_token: str
    shopee_app_id: str
    shopee_secret: str
    dry_run: bool

    @classmethod
    def from_env(cls, *, dry_run_override: bool | None = None) -> "Settings":
        load_dotenv(override=False)
        dry_run = (
            dry_run_override
            if dry_run_override is not None
            else _parse_bool(os.environ.get("DRY_RUN"), default=False)
        )
        return cls(
            gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
            gemini_primary_model=(
                os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_PRIMARY_MODEL
            ),
            google_sheet_id=os.environ.get(
                "GOOGLE_SHEET_ID", "1PZ024Tr8kONsIlKf68i2Y-YQLX3BFKhOmS3pvl2BRcc"
            ).strip(),
            google_service_account_json=os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip(),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            threads_user_id=os.environ.get("THREADS_USER_ID", "").strip(),
            threads_access_token=os.environ.get("THREADS_ACCESS_TOKEN", "").strip(),
            shopee_app_id=os.environ.get("SHOPEE_APP_ID", "").strip(),
            shopee_secret=os.environ.get("SHOPEE_SECRET", "").strip(),
            dry_run=dry_run,
        )

"""Client Google Gemini (`google-genai`) dengan degradasi otomatis bertahap.

Jika model tertinggi (mis. `gemini-4-hydrogen`) terkena rate-limit (429),
quota habis, butuh billing (402/403), atau tidak tersedia (404), client ini
otomatis menurunkan pilihan ke model gratis tertinggi berikutnya secara berurutan.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Type, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from src.config import DEFAULT_GEMINI_FALLBACK_CHAIN, DEFAULT_GEMINI_PRIMARY_MODEL, parse_model_chain

T = TypeVar("T", bound=BaseModel)


class GeminiClientError(RuntimeError):
    """Raised when all Gemini models in the fallback chain fail."""


@dataclass(frozen=True)
class GeminiCallMeta:
    requested_model: str
    actual_model: str
    used_fallback: bool
    attempted_models: tuple[str, ...]
    fallback_notes: str
    rate_limited_count: int = 0

    def display_label(self) -> str:
        if self.used_fallback:
            return f"{self.actual_model} (fallback dari {self.requested_model})"
        return self.actual_model

    def quota_warning_message(self) -> str | None:
        low_tier = self.used_fallback and (
            "lite" in self.actual_model.lower()
            or self.actual_model.lower().startswith("gemma-")
        )
        if self.rate_limited_count >= 2 or low_tier:
            return (
                f"Kuota Gemini API mulai menipis ({self.rate_limited_count}x limit 429, "
                f"aktif di {self.actual_model}). Disarankan buat GEMINI_API_KEY baru di "
                "Google Cloud Project terpisah dari Kimiabill_Project sebagai cadangan."
            )
        return None


def extract_json_object(text: str) -> dict[str, Any]:
    """Ekstrak objek JSON pertama dari output teks model."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    start = cleaned.find("{")
    if start == -1:
        raise ValueError(f"Tidak ditemukan JSON object pada respons Gemini: {cleaned[:240]}")

    decoder = json.JSONDecoder()
    for candidate in (cleaned[start:], re.sub(r",\s*([}\]])", r"\1", cleaned[start:])):
        try:
            obj, _ = decoder.raw_decode(candidate, 0)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    end = cleaned.rfind("}")
    if end <= start:
        raise ValueError(f"JSON object tidak lengkap pada respons Gemini: {cleaned[:240]}")
    tail_cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned[start : end + 1])
    parsed = json.loads(tail_cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Respons JSON Gemini bukan object dict.")
    return parsed


def _classify_error(exc: Exception) -> tuple[int | None, str]:
    """Ambil HTTP status code (jika ada) dan klasifikasi alasan kegagalan."""
    status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(status_code, str) and status_code.isdigit():
        status_code = int(status_code)
    msg = str(exc)
    low = msg.lower()

    if status_code is None:
        m = re.search(r"\b(400|401|402|403|404|429|500|502|503|504)\b", msg)
        if m:
            status_code = int(m.group(1))

    if status_code == 429 or "resource_exhausted" in low or "quota" in low or "rate limit" in low:
        return status_code or 429, "limit/kuota habis (429)"
    if status_code in (402, 403) or "billing" in low or "permission_denied" in low:
        return status_code or 403, "akses dibatasi/butuh billing (403)"
    if status_code == 404 or "not_found" in low or "is not found" in low:
        return status_code or 404, "model tidak tersedia (404)"
    if status_code in (500, 502, 503, 504) or "unavailable" in low or "overloaded" in low:
        return status_code or 503, f"gangguan sementara server ({status_code or 503})"
    return status_code, msg[:180]


def _should_downgrade_immediately(status_code: int | None, reason: str) -> bool:
    if status_code in (402, 403, 404, 429):
        return True
    low = reason.lower()
    return any(tok in low for tok in ("limit", "kuota", "quota", "billing", "tidak tersedia", "not found"))


def _is_transient(status_code: int | None) -> bool:
    return status_code in (500, 502, 503, 504)


class GeminiClient:
    """Wrapper `google-genai` dengan rantai degradasi model otomatis per run."""

    def __init__(
        self,
        api_key: str,
        *,
        primary_model: str = DEFAULT_GEMINI_PRIMARY_MODEL,
        fallback_chain: tuple[str, ...] = DEFAULT_GEMINI_FALLBACK_CHAIN,
        client: Any | None = None,
        sleep_fn: Any = time.sleep,
    ) -> None:
        if not api_key and client is None:
            raise GeminiClientError("GEMINI_API_KEY belum diisi.")
        self._client = client if client is not None else genai.Client(api_key=api_key)
        self.primary_model = (primary_model or DEFAULT_GEMINI_PRIMARY_MODEL).strip()
        self.fallback_chain = parse_model_chain(",".join(fallback_chain), primary=self.primary_model)
        self._exhausted_models: set[str] = set()
        self._sleep = sleep_fn

    def _discover_available_free_flash(self, already_tried: set[str]) -> list[str]:
        """Cari model Gemini/Gemma lain lewat API models.list jika daftar statis gagal semua."""
        try:
            discovered: list[str] = []
            for m in self._client.models.list():
                raw_name = getattr(m, "name", "") or ""
                name = raw_name.removeprefix("models/")
                methods = getattr(m, "supported_actions", None) or getattr(
                    m, "supported_generation_methods", []
                )
                if methods and "generateContent" not in methods:
                    continue
                if name.startswith(("gemini-", "gemma-")):
                    if not any(s in name for s in ("tts", "image", "audio", "live", "embedding", "robotics", "veo", "imagen")):
                        if name not in already_tried and name not in self._exhausted_models:
                            discovered.append(name)
            discovered.sort(
                key=lambda n: ("flash" in n, "lite" not in n, "preview" not in n, n),
                reverse=True,
            )
            return discovered[:8]
        except Exception:
            return []

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: Type[T],
        temperature: float = 0.7,
        max_transient_retries: int = 2,
        use_google_search: bool = False,
    ) -> tuple[T, GeminiCallMeta]:
        """Panggil Gemini dengan output JSON terstruktur & fallback otomatis ke model gratis tertinggi."""
        schema = response_model.model_json_schema()
        full_user_prompt = (
            f"{user_prompt}\n\n"
            "WAJIB: Balas HANYA berupa JSON valid sesuai skema berikut tanpa markdown tambahan:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )

        # Jika GoogleSearch grounding dibatasi kuota (sering 429 di free tier),
        # otomatis lakukan putaran kedua tanpa GoogleSearch grounding sebelum menyerah.
        search_modes = (True, False) if use_google_search else (False,)
        all_attempted: list[str] = []
        notes: list[str] = []
        standard_429_models: set[str] = set()

        for search_enabled in search_modes:
            candidates = [m for m in self.fallback_chain if m not in self._exhausted_models]
            if not candidates:
                candidates = list(self.fallback_chain)
            if not search_enabled and use_google_search:
                # Reset cache hanya untuk mencoba ulang tanpa grounding tool
                candidates = list(self.fallback_chain)

            discovered_added = False
            idx = 0
            while idx < len(candidates):
                model_name = candidates[idx]
                idx += 1
                all_attempted.append(model_name)

                if idx == len(candidates) and not discovered_added:
                    discovered_added = True
                    extra = self._discover_available_free_flash(set(candidates) | set(all_attempted))
                    for em in extra:
                        if em not in candidates:
                            candidates.append(em)

                allow_search_this_model = search_enabled
                for attempt in range(max_transient_retries + 1):
                    try:
                        config_kwargs: dict[str, Any] = {
                            "system_instruction": system_prompt,
                            "temperature": temperature,
                        }
                        if allow_search_this_model:
                            config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
                        else:
                            config_kwargs["response_mime_type"] = "application/json"

                        response = self._client.models.generate_content(
                            model=model_name,
                            contents=full_user_prompt,
                            config=types.GenerateContentConfig(**config_kwargs),
                        )
                        raw_text = getattr(response, "text", None) or ""
                        if not raw_text and getattr(response, "candidates", None):
                            parts = response.candidates[0].content.parts
                            raw_text = "".join(getattr(p, "text", "") or "" for p in parts)
                        parsed_dict = extract_json_object(raw_text)
                        parsed_obj = response_model.model_validate(parsed_dict)

                        used_fallback = model_name != self.primary_model or (
                            use_google_search and not allow_search_this_model
                        )
                        meta = GeminiCallMeta(
                            requested_model=self.primary_model,
                            actual_model=model_name,
                            used_fallback=used_fallback,
                            attempted_models=tuple(dict.fromkeys(all_attempted)),
                            fallback_notes=" | ".join(notes),
                            rate_limited_count=len(standard_429_models),
                        )
                        return parsed_obj, meta

                    except Exception as exc:
                        status_code, reason = _classify_error(exc)

                        if status_code == 401:
                            raise GeminiClientError(
                                "GEMINI_API_KEY tidak valid atau ditolak (HTTP 401)."
                            ) from exc

                        if allow_search_this_model and status_code in (400, 403):
                            allow_search_this_model = False
                            notes.append(f"{model_name} tanpa GoogleSearch ({reason})")
                            continue

                        if _should_downgrade_immediately(status_code, reason):
                            if not search_enabled:
                                self._exhausted_models.add(model_name)
                                if status_code == 429:
                                    standard_429_models.add(model_name)
                            msg = f"{model_name}{' [search]' if allow_search_this_model else ''} -> {reason}"
                            notes.append(msg)
                            print(f"[gemini] {msg}. Menurunkan ke model berikutnya...")
                            break

                        if _is_transient(status_code) and attempt < max_transient_retries:
                            wait_s = min(6, 2 * (attempt + 1))
                            print(
                                f"[gemini] {model_name} gangguan sementara ({reason}), "
                                f"coba ulang dalam {wait_s}s..."
                            )
                            self._sleep(wait_s)
                            continue

                        msg = f"{model_name} gagal ({reason})"
                        notes.append(msg)
                        print(f"[gemini] {msg}. Beralih ke model berikutnya...")
                        break

        raise GeminiClientError(
            "Semua model Gemini dalam rantai fallback gagal digunakan. "
            f"Dicoba: {', '.join(dict.fromkeys(all_attempted))}. Detail: {' | '.join(notes)}"
        )

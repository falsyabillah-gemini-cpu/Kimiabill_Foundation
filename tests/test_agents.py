"""Unit test untuk seluruh agent Kantor Agent Affiliate Online."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from src.config import Settings
from src.copywriter_agent import CopywritingDraft, generate_copywriting, run_qc_and_sanitize
from src.engagement_manager import check_token_age_warning, run_engagement_manager
from src.gemini_client import GeminiClient, GeminiClientError
from src.publishers import format_publish_telegram_message, publish_to_threads
from src.sheets_client import ProductRow, SheetsClient
from src.shopee_hunter import fetch_shopee_xtra_product, pick_next_product


class _SampleOut(BaseModel):
    answer: str


class _FakeModelsAPI:
    def __init__(self, behaviors: dict[str, list[Any]], listed_models: list[str] | None = None):
        self.behaviors = behaviors
        self.calls: list[str] = []
        self.listed_models = listed_models or []

    def generate_content(self, *, model: str, contents: str, config: Any) -> Any:
        self.calls.append(model)
        queue = self.behaviors.get(model, [])
        if not queue:
            raise RuntimeError(f"404 NOT_FOUND: model {model} not configured")
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=json.dumps(outcome))

    def list(self) -> list[Any]:
        return [
            SimpleNamespace(name=f"models/{m}", supported_actions=["generateContent"])
            for m in self.listed_models
        ]


class _FakeGenAIClient:
    def __init__(self, behaviors: dict[str, list[Any]], listed_models: list[str] | None = None):
        self.models = _FakeModelsAPI(behaviors, listed_models=listed_models)


class _FakeWorksheet:
    def __init__(self, rows: list[list[str]]):
        self.rows = [list(r) for r in rows]

    def get_all_values(self) -> list[list[str]]:
        return [list(r) for r in self.rows]

    def batch_update(self, data: list[dict[str, Any]], value_input_option: str = "USER_ENTERED") -> None:
        for item in data:
            ref = item["range"]
            col_letters = "".join(ch for ch in ref if ch.isalpha())
            row_num = int("".join(ch for ch in ref if ch.isdigit()))
            col_idx = 0
            for ch in col_letters:
                col_idx = col_idx * 26 + (ord(ch.upper()) - ord("A") + 1)
            col_idx -= 1
            while len(self.rows) < row_num:
                self.rows.append([])
            while len(self.rows[row_num - 1]) <= col_idx:
                self.rows[row_num - 1].append("")
            self.rows[row_num - 1][col_idx] = item["values"][0][0]

    def update(self, *, range_name: str, values: list[list[str]], value_input_option: str = "USER_ENTERED") -> None:
        start_cell = range_name.split(":")[0]
        row_num = int("".join(ch for ch in start_cell if ch.isdigit()))
        while len(self.rows) < row_num:
            self.rows.append([])
        self.rows[row_num - 1] = list(values[0])


class _FakeSpreadsheet:
    def __init__(self, tabs: dict[str, list[list[str]]]):
        self.sheets = {k: _FakeWorksheet(v) for k, v in tabs.items()}

    def worksheet(self, title: str) -> _FakeWorksheet:
        return self.sheets[title]


def _make_fake_sheets(pending_rows: list[list[str]] | None = None) -> SheetsClient:
    antrean_headers = [
        "ID", "Status", "Kategori", "Nama_Produk", "Harga_Kisaran",
        "Masalah_dan_Keunggulan", "Link_Affiliate", "URL_Foto_Publik",
        "Catatan_User_untuk_AI", "Sumber", "Waktu_Tayang_WIB",
        "Threads_Post_ID", "Link_Post_Threads", "Model_AI_Dipakai", "Catatan_Agent",
    ]
    log_headers = [
        "Tanggal_WIB", "ID_Produk", "Nama_Produk", "Mode_Run", "Status_Threads",
        "Threads_Post_ID", "Threads_Reply_ID", "Draft_Threads", "Draft_Reply_Link",
        "Draft_X", "Status_Telegram", "Model_AI_Dipakai", "Catatan_QC",
    ]
    komentar_headers = [
        "Tanggal_WIB", "Threads_Post_ID", "Comment_ID", "Username_Komentar",
        "Isi_Komentar", "Saran_Balasan_AI", "Model_AI_Dipakai",
        "Status_Balasan", "Balasan_Manual_User",
    ]
    pengaturan_rows = [
        ["Kunci", "Nilai", "Kategori_Setting", "Keterangan"],
        ["Niche_Utama", "Gadget Meja Kerja", "Konten", ""],
        ["Model_Gemini_Utama", "gemini-4-hydrogen", "AI Model", ""],
        ["Rantai_Fallback_Gemini", "gemini-4-hydrogen, gemini-3.8-flash, gemini-3.6-flash", "AI Model", ""],
        ["Kata_Kunci_Auto_Hunter", "lampu monitor", "Auto Hunter", ""],
        ["Min_Rating_Shopee", "4.7", "Auto Hunter", ""],
        ["Min_Stok_Peringatan", "5", "Monitoring", ""],
        ["Auto_Reply_Komentar", "OFF", "Engagement", ""],
        ["Tanggal_Token_Threads", "2026-09-01", "Opsional", ""],
    ]
    referensi_headers = [
        "Tanggal_Input", "Status_Riset", "Akun_atau_Sumber",
        "Teks_Postingan_Contoh", "Perkiraan_Engagement", "Catatan_Kenapa_Bagus",
    ]
    audit_headers = [
        "Tanggal_Audit_WIB", "Sumber_Riset", "Pola_Hook_Terbaik",
        "Pola_Kalimat_Human", "Pola_CTA_Link_Reply", "Pantangan_QC_Baru",
        "Ringkasan_Eksekutif", "Status_Feed_Copywriter", "Model_AI_Dipakai",
    ]
    ceo_headers = [
        "Tanggal_Audit_WIB", "Periode_Audit", "Skor_Kesehatan_Kantor", "Status_Kantor",
        "Ringkasan_Eksekutif_CEO", "Evaluasi_Konten_&_Hook", "Evaluasi_Model_AI_&_Kuota",
        "Rekomendasi_Aksi_CEO", "Rekomendasi_Prompt_Agent2", "Model_AI_Dipakai",
    ]
    tabs = {
        "Antrean_Produk": [antrean_headers] + (pending_rows or []),
        "Log_Performa": [log_headers],
        "Komentar_Threads": [komentar_headers],
        "Pengaturan_Agent": pengaturan_rows,
        "Referensi_Viral_Threads": [referensi_headers],
        "Audit_Riset_Threads": [audit_headers],
        "Laporan_Super_Audit_CEO": [ceo_headers],
    }
    return SheetsClient("sheet-123", "", spreadsheet=_FakeSpreadsheet(tabs))


def test_gemini_gradual_fallback_and_exhaustion_cache() -> None:
    fake = _FakeGenAIClient(
        {
            "gemini-4-hydrogen": [RuntimeError("429 RESOURCE_EXHAUSTED: quota limit")],
            "gemini-3.8-flash": [RuntimeError("403 PERMISSION_DENIED: billing required")],
            "gemini-3.6-flash": [{"answer": "ok1"}, {"answer": "ok2"}],
        }
    )
    client = GeminiClient(
        "dummy-key",
        primary_model="gemini-4-hydrogen",
        fallback_chain=("gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash"),
        client=fake,
        sleep_fn=lambda _: None,
    )

    res1, meta1 = client.generate_structured(
        system_prompt="sys", user_prompt="usr", response_model=_SampleOut
    )
    assert res1.answer == "ok1"
    assert meta1.requested_model == "gemini-4-hydrogen"
    assert meta1.actual_model == "gemini-3.6-flash"
    assert meta1.used_fallback is True
    assert meta1.attempted_models == ("gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash")

    # Panggilan kedua pada run yang sama langsung melompati model yang sudah exhausted
    res2, meta2 = client.generate_structured(
        system_prompt="sys", user_prompt="usr2", response_model=_SampleOut
    )
    assert res2.answer == "ok2"
    assert meta2.attempted_models == ("gemini-3.6-flash",)


def test_gemini_dynamic_discovery_when_static_models_fail() -> None:
    fake = _FakeGenAIClient(
        {
            "gemini-4-hydrogen": [RuntimeError("429 quota")],
            "gemini-3.9-flash": [{"answer": "discovered-ok"}],
        },
        listed_models=["gemini-3.9-flash", "gemini-2.5-flash-tts"],
    )
    client = GeminiClient(
        "dummy-key",
        primary_model="gemini-4-hydrogen",
        fallback_chain=("gemini-4-hydrogen",),
        client=fake,
        sleep_fn=lambda _: None,
    )
    # Override parse_model_chain additions for isolated discovery check
    client.fallback_chain = ("gemini-4-hydrogen",)
    res, meta = client.generate_structured(
        system_prompt="sys", user_prompt="usr", response_model=_SampleOut
    )
    assert res.answer == "discovered-ok"
    assert meta.actual_model == "gemini-3.9-flash"


def test_gemini_401_raises_immediately() -> None:
    fake = _FakeGenAIClient({"gemini-4-hydrogen": [RuntimeError("401 API key invalid")]})
    client = GeminiClient("bad", client=fake, sleep_fn=lambda _: None)
    with pytest.raises(GeminiClientError, match="401"):
        client.generate_structured(system_prompt="s", user_prompt="u", response_model=_SampleOut)


def test_copywriter_qc_strips_leaked_link_and_ensures_reply_has_link() -> None:
    link = "https://s.shopee.co.id/abc123"
    draft = CopywritingDraft(
        threads_main_post=f"Meja berantakan? Pakai klip ini {link} dijamin rapi!",
        threads_reply_text="Beli di sini ya kak",
        x_draft_post="Klip kabel magnetik anti jatuh.",
    )
    sanitized, qc_ok, note = run_qc_and_sanitize(draft, link)
    assert qc_ok is False
    assert "shopee" not in sanitized.threads_main_post.lower()
    assert link in sanitized.threads_reply_text
    assert link in sanitized.x_draft_post
    assert "dibersihkan" in note


def test_copywriter_retries_once_on_link_leak() -> None:
    link = "https://s.shopee.co.id/xyz999"
    fake = _FakeGenAIClient(
        {
            "gemini-4-hydrogen": [
                {
                    "threads_main_post": f"Cek barang ini di {link}",
                    "threads_reply_text": f"Link: {link}",
                    "x_draft_post": f"Rekomendasi meja: {link}",
                },
                {
                    "threads_main_post": "Kabel charger sering jatuh? Pakai klip magnetik ini, cek komentar ya!",
                    "threads_reply_text": f"Ini link resminya: {link}",
                    "x_draft_post": f"Klip kabel magnetik praktis. {link}",
                },
            ]
        }
    )
    gemini = GeminiClient("k", client=fake, sleep_fn=lambda _: None)
    product = ProductRow(
        row_number=2,
        product_id="1",
        status="PENDING",
        kategori="Meja Kerja",
        nama_produk="Kabel Organizer",
        harga_kisaran="Rp25.000",
        masalah_dan_keunggulan="Kabel rapi tidak jatuh ke kolong",
        link_affiliate=link,
    )
    res = generate_copywriting(gemini, product, {})
    assert res.qc_passed is True
    assert link not in res.draft.threads_main_post
    assert link in res.draft.threads_reply_text


def test_hunter_marks_invalid_row_error_and_picks_next_valid() -> None:
    sheets = _make_fake_sheets(
        [
            ["1", "PENDING", "Meja", "Produk Rusak", "", "Desc", "https://s.shopee.co.id/CONTOH_GANTI_LINK_ANDA", "", "", "SHEET_MANUAL", "", "", "", "", ""],
            ["2", "PENDING", "Meja", "Lampu Monitor", "Rp199.000", "Terang merata tidak silau", "https://s.shopee.co.id/lampu123", "", "", "SHEET_MANUAL", "", "", "", "", ""],
        ]
    )
    settings = Settings.from_env(dry_run_override=True)
    chosen = pick_next_product(sheets, settings, sheets.get_settings())
    assert chosen is not None
    assert chosen.nama_produk == "Lampu Monitor"
    rows = sheets.list_queue_rows()
    assert rows[0].status == "ERROR"
    assert rows[1].status == "PENDING"  # dry_run keeps PENDING


def test_shopee_auto_hunter_filters_rating_and_commission() -> None:
    class _FakeResp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "data": {
                    "productOfferV2": {
                        "nodes": [
                            {
                                "productName": "Produk Rating Rendah",
                                "ratingStar": 4.2,
                                "sellerCommissionRate": 0.1,
                                "offerLink": "https://s.shopee.co.id/low",
                                "priceMin": 50000,
                            },
                            {
                                "productName": "Lampu Monitor Baseus",
                                "ratingStar": 4.9,
                                "sellerCommissionRate": 0.08,
                                "commissionRate": 0.02,
                                "sales": 1200,
                                "offerLink": "https://s.shopee.co.id/lampu",
                                "imageUrl": "https://cf.shopee.co.id/img.jpg",
                                "priceMin": 249000,
                            },
                        ]
                    }
                }
            }

    found = fetch_shopee_xtra_product(
        app_id="123",
        secret="sec",
        keywords=["lampu monitor"],
        min_rating=4.7,
        http_post=lambda *a, **kw: _FakeResp(),
    )
    assert found is not None
    assert found["nama_produk"] == "Lampu Monitor Baseus"
    assert found["harga_kisaran"] == "Rp249.000"


def test_publish_to_threads_live_and_telegram_escaping() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, data: dict[str, Any] | None = None, **kw: Any) -> Any:
        calls.append((url, data or {}))
        if url.endswith("/threads"):
            cid = "c_reply" if "reply_to_id" in (data or {}) else "c_main"
            return SimpleNamespace(status_code=200, json=lambda: {"id": cid})
        if url.endswith("/threads_publish"):
            pid = "p_reply" if (data or {}).get("creation_id") == "c_reply" else "p_main"
            return SimpleNamespace(status_code=200, json=lambda: {"id": pid})
        raise AssertionError(url)

    def fake_get(url: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        if url.endswith("/p_main"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"id": "p_main", "permalink": "https://www.threads.net/@user/post/123"},
            )
        return SimpleNamespace(status_code=200, json=lambda: {"status": "FINISHED"})

    res = publish_to_threads(
        user_id="u1",
        access_token="tok",
        main_post="Utas tanpa link",
        reply_text="Link: https://s.shopee.co.id/abc",
        image_url="https://example.com/a.jpg",
        dry_run=False,
        http_post=fake_post,
        http_get=fake_get,
        sleep_fn=lambda _: None,
    )
    assert res.status == "PUBLISHED"
    assert res.post_id == "p_main"
    assert res.reply_id == "p_reply"
    assert "threads.net" in res.permalink
    assert calls[0][1]["media_type"] == "IMAGE"
    assert calls[2][1]["reply_to_id"] == "p_main"

    tg_msg = format_publish_telegram_message(
        product_name="Rak <Sudut> & Meja",
        kategori="Kos",
        threads_result=res,
        threads_main_post="Main",
        threads_reply_text="Reply",
        x_draft_post="X draft",
        model_label="gemini-3.8-flash (fallback dari gemini-4-hydrogen)",
        qc_notes="OK",
        pending_remaining=2,
        min_stock=5,
        dry_run=False,
    )
    assert "Rak &lt;Sudut&gt; &amp; Meja" in tg_msg
    assert "Di bawah batas minimum 5" in tg_msg


def test_token_age_warning_and_engagement_dedup() -> None:
    assert check_token_age_warning("2026-09-01", today=date(2026, 11, 24)) is not None
    assert check_token_age_warning("2026-11-10", today=date(2026, 11, 24)) is None

    sheets = _make_fake_sheets()
    sheets.append_performance_log(
        {
            "Tanggal_WIB": "2026-11-24 12:00:00 WIB",
            "ID_Produk": "1",
            "Nama_Produk": "Lampu Monitor",
            "Mode_Run": "LIVE",
            "Status_Threads": "PUBLISHED",
            "Threads_Post_ID": "post_1",
            "Threads_Reply_ID": "rep_own",
            "Draft_Threads": "Utas",
            "Draft_Reply_Link": "https://s.shopee.co.id/abc",
            "Draft_X": "X",
            "Status_Telegram": "SENT",
            "Model_AI_Dipakai": "gemini-4-hydrogen",
            "Catatan_QC": "OK",
        }
    )

    fake_ai = _FakeGenAIClient(
        {"gemini-4-hydrogen": [{"reply_text": "Halo kak! Linknya sudah aku taruh di balasan pertama ya 🙌"}]}
    )
    gemini = GeminiClient("k", client=fake_ai, sleep_fn=lambda _: None)

    def fake_get(url: str, **kw: Any) -> Any:
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "data": [
                    {"id": "rep_own", "text": "Cek di sini https://s.shopee.co.id/abc", "username": "me"},
                    {"id": "rep_user1", "text": "Kak ini wattnya berapa ya?", "username": "pembeli_1"},
                ]
            },
        )

    tg_payloads: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any] | None = None, **kw: Any) -> Any:
        tg_payloads.append(json or {})
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    settings = Settings(
        gemini_api_key="k",
        gemini_primary_model="gemini-4-hydrogen",
        google_sheet_id="sheet-123",
        google_service_account_json="",
        telegram_bot_token="bot",
        telegram_chat_id="123",
        threads_user_id="u1",
        threads_access_token="tok",
        shopee_app_id="",
        shopee_secret="",
        dry_run=False,
    )

    summary = run_engagement_manager(
        settings=settings,
        sheets=sheets,
        gemini=gemini,
        http_get=fake_get,
        http_post=fake_post,
    )
    assert summary["new_comments"] == 1
    assert summary["telegram_status"] == "SENT"
    assert "pembeli_1" in tg_payloads[0]["text"]
    assert "rep_user1" in sheets.list_existing_comment_ids()


def test_csv_importer_parse_and_dedup(tmp_path: Any) -> None:
    from src.csv_importer import import_csv_to_sheets, normalize_shopee_price

    assert normalize_shopee_price("48,8RB") == "Rp48.800"
    assert normalize_shopee_price("1,4JT") == "Rp1.400.000"

    csv_file = tmp_path / "shopee_bulk.csv"
    csv_file.write_text(
        "ID Produk,Nama Produk,Harga,Penjualan,Nama Toko,Komisi hingga,Komisi,Link Produk,Link Komisi Ekstra\n"
        '101,(HOT)BASIKE Charger 40W,"54,5RB",10RB+,Basike Official,"18,5%",Rp9.810,https://shopee.co.id/p1,https://s.shopee.co.id/short1\n'
        '102,Lampu Meja Clamp LED,"129,0RB",1RB+,Michizuru,"11,5%",Rp14.190,https://shopee.co.id/p2,https://s.shopee.co.id/short2\n',
        encoding="utf-8",
    )

    sheets = _make_fake_sheets()
    first = import_csv_to_sheets(sheets, csv_file)
    assert first == {"total_csv": 2, "added": 2, "skipped": 0}

    rows = sheets.list_queue_rows()
    assert rows[0].status == "PENDING"
    assert rows[0].nama_produk == "BASIKE Charger 40W"
    assert rows[0].harga_kisaran == "Rp54.500"
    assert rows[0].link_affiliate == "https://s.shopee.co.id/short1"
    assert rows[0].sumber == "CSV_BULK_IMPORT"

    second = import_csv_to_sheets(sheets, csv_file)
    assert second == {"total_csv": 2, "added": 0, "skipped": 2}


def test_agent6_trend_researcher_feeds_copywriter_and_adds_sheet_link() -> None:
    from src.trend_researcher import run_trend_researcher

    sheets = _make_fake_sheets()
    sheets._spreadsheet.worksheet("Referensi_Viral_Threads").update(
        range_name="A2:F2",
        values=[[
            "2026-11-25",
            "AKTIF",
            "@affiliate_pro",
            "Jujur nyesel baru tau ada klip kabel magnetik gini, meja kerja langsung rapi.",
            "300 likes",
            "Hook jujur & natural",
        ]],
    )

    fake_ai = _FakeGenAIClient(
        {
            "gemini-4-hydrogen": [
                RuntimeError("400 INVALID_ARGUMENT: google_search tool unsupported"),
                {
                    "pola_hook_terbaik": "1. Pengakuan jujur\n2. Pertanyaan relate",
                    "pola_kalimat_human": "Pakai kalimat pendek dan cerita nyata.",
                    "pola_cta_link_reply": "Linknya aku taro di balasan ya.",
                    "pantangan_qc": ["jangan lewatkan", "klik keranjang kuning"],
                    "ringkasan_eksekutif": "Konten bergaya curhat singkat lebih tinggi reply-nya.",
                },
            ]
        }
    )
    gemini = GeminiClient("k", client=fake_ai, sleep_fn=lambda _: None)

    def fake_get(url: str, **kw: Any) -> Any:
        if url.endswith("/threads"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"data": [{"id": "p1", "text": "Contoh post sendiri"}]},
            )
        if "/insights" in url:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"data": [{"name": "views", "values": [{"value": 1200}]}]},
            )
        return SimpleNamespace(status_code=200, json=lambda: {"data": []})

    tg_payloads: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any] | None = None, **kw: Any) -> Any:
        tg_payloads.append(json or {})
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    settings = Settings(
        gemini_api_key="k",
        gemini_primary_model="gemini-4-hydrogen",
        google_sheet_id="sheet-123",
        google_service_account_json="",
        telegram_bot_token="bot",
        telegram_chat_id="123",
        threads_user_id="u1",
        threads_access_token="tok",
        shopee_app_id="",
        shopee_secret="",
        dry_run=False,
    )

    res = run_trend_researcher(
        settings=settings,
        sheets=sheets,
        gemini=gemini,
        http_get=fake_get,
        http_post=fake_post,
    )
    assert res["telegram_status"] == "SENT"
    assert "docs.google.com/spreadsheets/d/sheet-123/edit" in tg_payloads[0]["text"]

    latest_audit = sheets.get_latest_active_audit()
    assert latest_audit is not None
    assert "klik keranjang kuning" in latest_audit["Pantangan_QC_Baru"]

    # Verifikasi QC Copywriter menangkap frasa pantangan dari Agent 6 lalu rewrite
    draft = CopywritingDraft(
        threads_main_post="Meja rapi! Jangan lewatkan, langsung klik keranjang kuning sekarang.",
        threads_reply_text="Ini linknya https://s.shopee.co.id/test1",
        x_draft_post="Meja rapi https://s.shopee.co.id/test1",
    )
    from src.copywriter_agent import parse_banned_phrases

    banned = parse_banned_phrases(latest_audit["Pantangan_QC_Baru"])
    _, qc_ok, qc_notes = run_qc_and_sanitize(
        draft, "https://s.shopee.co.id/test1", banned_phrases=banned
    )
    assert qc_ok is False
    assert "frasa kaku/iklan" in qc_notes

    tg_pub = format_publish_telegram_message(
        product_name="Kabel Klip",
        kategori="Meja",
        threads_result=SimpleNamespace(status="PUBLISHED", permalink="https://threads.com/p/1", error_message=""),
        threads_main_post="Main",
        threads_reply_text="Reply",
        x_draft_post="X",
        model_label="gemini-4-hydrogen",
        qc_notes="OK",
        pending_remaining=10,
        min_stock=5,
        dry_run=False,
        sheet_link="https://docs.google.com/spreadsheets/d/sheet-123/edit",
    )
    assert "https://docs.google.com/spreadsheets/d/sheet-123/edit" in tg_pub


def test_gemini_low_quota_warning_triggered_and_formatted() -> None:
    fake = _FakeGenAIClient(
        {
            "gemini-4-hydrogen": [RuntimeError("429 RESOURCE_EXHAUSTED")],
            "gemini-3.8-flash": [RuntimeError("429 RESOURCE_EXHAUSTED")],
            "gemini-3.6-flash": [{"answer": "ok"}],
        }
    )
    client = GeminiClient(
        "dummy-key",
        primary_model="gemini-4-hydrogen",
        fallback_chain=("gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash"),
        client=fake,
        sleep_fn=lambda _: None,
    )
    _, meta = client.generate_structured(
        system_prompt="s", user_prompt="u", response_model=_SampleOut
    )
    warn = meta.quota_warning_message()
    assert warn is not None
    assert "GEMINI_API_KEY terpisah" in warn

    msg = format_publish_telegram_message(
        product_name="Kabel Klip",
        kategori="Meja",
        threads_result=SimpleNamespace(status="PUBLISHED", permalink="", error_message=""),
        threads_main_post="Main",
        threads_reply_text="Reply",
        x_draft_post="X",
        model_label=meta.display_label(),
        qc_notes="OK",
        pending_remaining=10,
        min_stock=5,
        dry_run=False,
        quota_warning=warn,
    )
    assert "PERINGATAN KUOTA GEMINI" in msg
    assert "Kimiabill_Project" in msg
    assert "aistudio.google.com/apikey" in msg


def test_openrouter_fallback_and_tutorial() -> None:
    fake = _FakeGenAIClient(
        {"gemini-4-hydrogen": [RuntimeError("429 RESOURCE_EXHAUSTED")]}
    )

    def fake_or_post(url: str, headers: dict[str, str], json: dict[str, Any], timeout: int) -> Any:
        assert "openrouter.ai" in url
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": '{"answer": "via-openrouter"}'}}]
            },
        )

    client = GeminiClient(
        "dummy-gemini",
        primary_model="gemini-4-hydrogen",
        fallback_chain=("gemini-4-hydrogen",),
        openrouter_api_key="or-key",
        openrouter_models=("google/gemini-2.5-flash",),
        client=fake,
        sleep_fn=lambda _: None,
        http_post=fake_or_post,
    )
    client.fallback_chain = ("gemini-4-hydrogen",)
    res, meta = client.generate_structured(
        system_prompt="s", user_prompt="u", response_model=_SampleOut
    )
    assert res.answer == "via-openrouter"
    assert meta.actual_model == "openrouter/google/gemini-2.5-flash"
    warn = meta.quota_warning_message()
    assert warn is not None
    assert "Create API key in new project" in warn


def test_agent7_super_audit_ceo() -> None:
    from src.ceo_auditor import run_super_audit

    sheets = _make_fake_sheets(
        [
            ["1", "PENDING", "Meja Kerja", "Kabel Klip", "Rp25.000", "Rapi", "https://s.shopee.co.id/1", "", "", "CSV_BULK_IMPORT", "", "", "", "", ""],
        ]
    )
    sheets.append_performance_log(
        {
            "Tanggal_WIB": "2026-11-25 21:21:47 WIB",
            "ID_Produk": "6",
            "Nama_Produk": "Mipanda Watch Charger",
            "Mode_Run": "LIVE",
            "Status_Threads": "PUBLISHED",
            "Threads_Post_ID": "p1",
            "Threads_Reply_ID": "r1",
            "Draft_Threads": "Contoh utas",
            "Draft_Reply_Link": "https://s.shopee.co.id/2",
            "Draft_X": "Contoh X",
            "Status_Telegram": "SENT",
            "Model_AI_Dipakai": "gemini-3.6-flash (fallback dari gemini-4-hydrogen)",
            "Catatan_QC": "OK",
        }
    )

    fake_ai = _FakeGenAIClient(
        {
            "gemini-4-hydrogen": [
                {
                    "skor_kesehatan_kantor": 88,
                    "status_kantor": "SEHAT",
                    "ringkasan_eksekutif_ceo": "Operasional minggu ini sehat dan 1 post sudah live.",
                    "evaluasi_konten_dan_hook": "Hook curhat meja kerja cukup menarik.",
                    "evaluasi_model_ai_dan_kuota": "Fallback berjalan lancar ke gemini-3.6-flash.",
                    "rekomendasi_aksi_ceo": "1. Tambah produk Meja Kerja\n2. Pantau engagement besok.",
                    "rekomendasi_prompt_agent2": "Pertahankan gaya curhat singkat.",
                }
            ]
        }
    )
    gemini = GeminiClient("k", client=fake_ai, sleep_fn=lambda _: None)

    def fake_get(url: str, **kw: Any) -> Any:
        if url.endswith("/threads"):
            return SimpleNamespace(status_code=200, json=lambda: {"data": [{"id": "p1", "text": "Post 1"}]})
        if "/insights" in url:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"data": [{"name": "views", "values": [{"value": 250}]}]},
            )
        return SimpleNamespace(status_code=200, json=lambda: {"data": []})

    tg_payloads: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any] | None = None, **kw: Any) -> Any:
        tg_payloads.append(json or {})
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    settings = Settings(
        gemini_api_key="k",
        gemini_primary_model="gemini-4-hydrogen",
        google_sheet_id="sheet-123",
        google_service_account_json="",
        telegram_bot_token="bot",
        telegram_chat_id="123",
        threads_user_id="u1",
        threads_access_token="tok",
        shopee_app_id="",
        shopee_secret="",
        dry_run=False,
    )

    res = run_super_audit(
        settings=settings, sheets=sheets, gemini=gemini, http_get=fake_get, http_post=fake_post
    )
    assert res["score"] == 88
    assert res["status"] == "SEHAT"
    assert res["telegram_status"] == "SENT"
    assert "LAPORAN SUPER AUDIT" in tg_payloads[0]["text"]
    assert "Laporan_Super_Audit_CEO" in tg_payloads[0]["text"]
    ceo_rows = sheets._spreadsheet.worksheet("Laporan_Super_Audit_CEO").get_all_values()
    assert len(ceo_rows) == 2
    assert ceo_rows[1][2] == "88"





"""Client Google Sheets untuk membaca antrean, pengaturan, dan menulis laporan/komentar."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TAB_ANTREAN = "Antrean_Produk"
TAB_LOG = "Log_Performa"
TAB_KOMENTAR = "Komentar_Threads"
TAB_PENGATURAN = "Pengaturan_Agent"
TAB_REFERENSI = "Referensi_Viral_Threads"
TAB_AUDIT = "Audit_Riset_Threads"
TAB_CEO_AUDIT = "Laporan_Super_Audit_CEO"


@dataclass
class ProductRow:
    row_number: int
    product_id: str
    status: str
    kategori: str
    nama_produk: str
    harga_kisaran: str
    masalah_dan_keunggulan: str
    link_affiliate: str
    url_foto_publik: str = ""
    catatan_user_untuk_ai: str = ""
    sumber: str = "SHEET_MANUAL"


def _load_credentials(service_account_source: str) -> Credentials:
    if not service_account_source:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON belum diisi.")
    stripped = service_account_source.strip()
    if stripped.startswith("{"):
        info = json.loads(stripped)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    path = Path(stripped)
    if path.exists():
        return Credentials.from_service_account_file(str(path), scopes=SCOPES)
    raise ValueError(
        "GOOGLE_SERVICE_ACCOUNT_JSON tidak valid (bukan JSON string maupun path file yang ada)."
    )


def _col_letter(col_idx_1based: int) -> str:
    result = ""
    n = col_idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


class SheetsClient:
    """Wrapper `gspread` untuk `Kantor Affiliate - Antrean & Laporan`."""

    def __init__(
        self,
        sheet_id: str,
        service_account_json: str,
        *,
        spreadsheet: Any | None = None,
    ) -> None:
        self.sheet_id = sheet_id
        if spreadsheet is not None:
            self._spreadsheet = spreadsheet
        else:
            creds = _load_credentials(service_account_json)
            gc = gspread.authorize(creds)
            self._spreadsheet = gc.open_by_key(sheet_id)

    def _ws(self, title: str) -> Any:
        return self._spreadsheet.worksheet(title)

    def get_settings(self) -> dict[str, str]:
        """Baca tab `Pengaturan_Agent` sebagai dict `{Kunci: Nilai}`."""
        ws = self._ws(TAB_PENGATURAN)
        rows = ws.get_all_values()
        if not rows:
            return {}
        headers = [h.strip() for h in rows[0]]
        key_idx = headers.index("Kunci") if "Kunci" in headers else 0
        val_idx = headers.index("Nilai") if "Nilai" in headers else 1

        settings: dict[str, str] = {}
        for row in rows[1:]:
            key = row[key_idx].strip() if len(row) > key_idx else ""
            val = row[val_idx].strip() if len(row) > val_idx else ""
            if key:
                settings[key] = val
        return settings

    def _read_table(self, title: str) -> tuple[Any, list[str], list[list[str]]]:
        ws = self._ws(title)
        rows = ws.get_all_values()
        if not rows:
            return ws, [], []
        headers = [h.strip() for h in rows[0]]
        return ws, headers, rows[1:]

    @staticmethod
    def _row_to_dict(headers: list[str], row: list[str]) -> dict[str, str]:
        return {
            header: (row[idx].strip() if idx < len(row) else "")
            for idx, header in enumerate(headers)
            if header
        }

    def list_queue_rows(self) -> list[ProductRow]:
        """Ambil seluruh baris non-kosong dari `Antrean_Produk`."""
        _, headers, data_rows = self._read_table(TAB_ANTREAN)
        products: list[ProductRow] = []
        for offset, raw in enumerate(data_rows, start=2):
            if not any(cell.strip() for cell in raw):
                continue
            item = self._row_to_dict(headers, raw)
            products.append(
                ProductRow(
                    row_number=offset,
                    product_id=item.get("ID") or str(offset - 1),
                    status=item.get("Status", "").upper(),
                    kategori=item.get("Kategori", ""),
                    nama_produk=item.get("Nama_Produk", ""),
                    harga_kisaran=item.get("Harga_Kisaran", ""),
                    masalah_dan_keunggulan=item.get("Masalah_dan_Keunggulan", ""),
                    link_affiliate=item.get("Link_Affiliate", ""),
                    url_foto_publik=item.get("URL_Foto_Publik", ""),
                    catatan_user_untuk_ai=item.get("Catatan_User_untuk_AI", ""),
                    sumber=item.get("Sumber", "") or "SHEET_MANUAL",
                )
            )
        return products

    def count_pending_products(self) -> int:
        return sum(1 for p in self.list_queue_rows() if p.status == "PENDING")

    def next_product_id(self) -> str:
        existing_ids: list[int] = []
        for p in self.list_queue_rows():
            if p.product_id.isdigit():
                existing_ids.append(int(p.product_id))
        return str((max(existing_ids) if existing_ids else 0) + 1)

    def update_queue_row_fields(self, row_number: int, updates: dict[str, str]) -> None:
        """Update kolom spesifik berdasarkan nama header pada baris `row_number`."""
        ws, headers, _ = self._read_table(TAB_ANTREAN)
        batch_payload: list[dict[str, Any]] = []
        for col_name, value in updates.items():
            if col_name not in headers:
                continue
            col_idx = headers.index(col_name) + 1
            cell_ref = f"{_col_letter(col_idx)}{row_number}"
            batch_payload.append({"range": cell_ref, "values": [[value]]})
        if batch_payload:
            ws.batch_update(batch_payload, value_input_option="USER_ENTERED")

    def append_queue_product(
        self,
        *,
        status: str,
        kategori: str,
        nama_produk: str,
        harga_kisaran: str,
        masalah_dan_keunggulan: str,
        link_affiliate: str,
        url_foto_publik: str = "",
        catatan_user_untuk_ai: str = "",
        sumber: str = "SHOPEE_AUTO_HUNTER",
        catatan_agent: str = "",
    ) -> ProductRow:
        """Tambahkan produk baru ke baris kosong pertama di `Antrean_Produk`."""
        ws, headers, data_rows = self._read_table(TAB_ANTREAN)
        product_id = self.next_product_id()
        values_by_col = {
            "ID": product_id,
            "Status": status,
            "Kategori": kategori,
            "Nama_Produk": nama_produk,
            "Harga_Kisaran": harga_kisaran,
            "Masalah_dan_Keunggulan": masalah_dan_keunggulan,
            "Link_Affiliate": link_affiliate,
            "URL_Foto_Publik": url_foto_publik,
            "Catatan_User_untuk_AI": catatan_user_untuk_ai,
            "Sumber": sumber,
            "Waktu_Tayang_WIB": "",
            "Threads_Post_ID": "",
            "Link_Post_Threads": "",
            "Model_AI_Dipakai": "",
            "Catatan_Agent": catatan_agent,
        }
        row_values = [values_by_col.get(h, "") for h in headers]

        # Cari baris pertama yang benar-benar kosong (karena baris 2-100 sudah diberi styling)
        target_row: int | None = None
        for offset, raw in enumerate(data_rows, start=2):
            if not any(cell.strip() for cell in raw):
                target_row = offset
                break
        if target_row is None:
            target_row = len(data_rows) + 2

        end_col = _col_letter(len(headers))
        ws.update(
            range_name=f"A{target_row}:{end_col}{target_row}",
            values=[row_values],
            value_input_option="USER_ENTERED",
        )
        return ProductRow(
            row_number=target_row,
            product_id=product_id,
            status=status,
            kategori=kategori,
            nama_produk=nama_produk,
            harga_kisaran=harga_kisaran,
            masalah_dan_keunggulan=masalah_dan_keunggulan,
            link_affiliate=link_affiliate,
            url_foto_publik=url_foto_publik,
            catatan_user_untuk_ai=catatan_user_untuk_ai,
            sumber=sumber,
        )

    def append_performance_log(self, entry: dict[str, str]) -> None:
        """Tambahkan baris riwayat eksekusi ke `Log_Performa`."""
        ws, headers, data_rows = self._read_table(TAB_LOG)
        row_values = [entry.get(h, "") for h in headers]
        target_row = self._first_empty_row(data_rows)
        end_col = _col_letter(len(headers))
        ws.update(
            range_name=f"A{target_row}:{end_col}{target_row}",
            values=[row_values],
            value_input_option="USER_ENTERED",
        )

    def list_published_post_ids(self, limit: int = 10) -> list[dict[str, str]]:
        """Ambil daftar `Threads_Post_ID` terbaru yang sudah tayang untuk dicek komentarnya."""
        _, headers, data_rows = self._read_table(TAB_LOG)
        posts: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in reversed(data_rows):
            if not any(c.strip() for c in raw):
                continue
            item = self._row_to_dict(headers, raw)
            post_id = item.get("Threads_Post_ID", "")
            status_threads = item.get("Status_Threads", "")
            if post_id and status_threads == "PUBLISHED" and post_id not in seen:
                seen.add(post_id)
                posts.append(item)
                if len(posts) >= limit:
                    break
        return posts

    def list_existing_comment_ids(self) -> set[str]:
        _, headers, data_rows = self._read_table(TAB_KOMENTAR)
        ids: set[str] = set()
        for raw in data_rows:
            if not any(c.strip() for c in raw):
                continue
            item = self._row_to_dict(headers, raw)
            cid = item.get("Comment_ID", "")
            if cid:
                ids.add(cid)
        return ids

    def append_comment_row(self, entry: dict[str, str]) -> None:
        ws, headers, data_rows = self._read_table(TAB_KOMENTAR)
        row_values = [entry.get(h, "") for h in headers]
        target_row = self._first_empty_row(data_rows)
        end_col = _col_letter(len(headers))
        ws.update(
            range_name=f"A{target_row}:{end_col}{target_row}",
            values=[row_values],
            value_input_option="USER_ENTERED",
        )

    def list_active_viral_references(self, limit: int = 15) -> list[dict[str, str]]:
        """Ambil contoh postingan viral berstatus `AKTIF` dari `Referensi_Viral_Threads`."""
        try:
            _, headers, data_rows = self._read_table(TAB_REFERENSI)
        except Exception:
            return []
        refs: list[dict[str, str]] = []
        for raw in reversed(data_rows):
            if not any(c.strip() for c in raw):
                continue
            item = self._row_to_dict(headers, raw)
            if item.get("Status_Riset", "").upper() == "AKTIF" and item.get("Teks_Postingan_Contoh"):
                refs.append(item)
                if len(refs) >= limit:
                    break
        return refs

    def append_audit_report(self, entry: dict[str, str]) -> None:
        """Simpan laporan audit riset baru ke tab `Audit_Riset_Threads`."""
        ws, headers, data_rows = self._read_table(TAB_AUDIT)
        row_values = [entry.get(h, "") for h in headers]
        target_row = self._first_empty_row(data_rows)
        end_col = _col_letter(len(headers))
        ws.update(
            range_name=f"A{target_row}:{end_col}{target_row}",
            values=[row_values],
            value_input_option="USER_ENTERED",
        )

    def get_latest_active_audit(self) -> dict[str, str] | None:
        """Ambil laporan audit riset terbaru yang berstatus `AKTIF_DIPAKAI` untuk Agent 2."""
        try:
            _, headers, data_rows = self._read_table(TAB_AUDIT)
        except Exception:
            return None
        for raw in reversed(data_rows):
            if not any(c.strip() for c in raw):
                continue
            item = self._row_to_dict(headers, raw)
            status = item.get("Status_Feed_Copywriter", "").upper()
            if status in ("AKTIF_DIPAKAI", ""):
                if item.get("Pola_Hook_Terbaik") or item.get("Pola_Kalimat_Human"):
                    return item
        return None

    def list_performance_logs(self, limit: int = 50) -> list[dict[str, str]]:
        """Ambil daftar log eksekusi terbaru dari `Log_Performa`."""
        try:
            _, headers, data_rows = self._read_table(TAB_LOG)
        except Exception:
            return []
        logs: list[dict[str, str]] = []
        for raw in reversed(data_rows):
            if not any(c.strip() for c in raw):
                continue
            logs.append(self._row_to_dict(headers, raw))
            if len(logs) >= limit:
                break
        return logs

    def list_comment_rows(self, limit: int = 50) -> list[dict[str, str]]:
        """Ambil daftar komentar terbaru dari `Komentar_Threads`."""
        try:
            _, headers, data_rows = self._read_table(TAB_KOMENTAR)
        except Exception:
            return []
        rows: list[dict[str, str]] = []
        for raw in reversed(data_rows):
            if not any(c.strip() for c in raw):
                continue
            rows.append(self._row_to_dict(headers, raw))
            if len(rows) >= limit:
                break
        return rows

    def append_ceo_audit_report(self, entry: dict[str, str]) -> None:
        """Simpan laporan Super Audit (Asisten CEO) ke tab `Laporan_Super_Audit_CEO`."""
        ws, headers, data_rows = self._read_table(TAB_CEO_AUDIT)
        row_values = [entry.get(h, "") for h in headers]
        target_row = self._first_empty_row(data_rows)
        end_col = _col_letter(len(headers))
        ws.update(
            range_name=f"A{target_row}:{end_col}{target_row}",
            values=[row_values],
            value_input_option="USER_ENTERED",
        )

    @staticmethod
    def _first_empty_row(data_rows: list[list[str]]) -> int:
        for offset, raw in enumerate(data_rows, start=2):
            if not any(cell.strip() for cell in raw):
                return offset
        return len(data_rows) + 2

# -*- coding: utf-8 -*-
"""
reports_loader.py

Metinių ataskaitų užkrovimo modulis Rinkos pulsas projektui.

Paskirtis:
- iš Supabase market_news paima CRIB kategorijos „Metinė informacija“ pranešimus;
- filtruoja tik market_issuers lentelėje esančius VLN emitentus iš Baltijos oficialiojo ir papildomojo sąrašo;
- randa CRIB priedus: PDF, ZIP, XHTML, XBRL, XML;
- atsisiunčia ir išsaugo failus į Supabase Storage bucket annual-reports;
- sukuria / atnaujina annual_reports ir annual_report_files lenteles;
- ZIP failus išarchyvuoja ir išsaugo vidinius failus atskirai;
- PDF tekstą, jei įmanoma, išsaugo raw_text lauke ateities rodiklių ištraukimui.

Šis failas gali būti naudojamas:
1) Streamlit puslapyje per metines.py;
2) lokaliai per terminalą:
   python reports_loader.py --start 2024-01-01 --end 2025-12-31
"""

import argparse
import hashlib
import mimetypes
import os
import re
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    from supabase_cache import _supabase_headers, _supabase_rest_url, _http_client, load_news_df
except Exception:
    _supabase_headers = None
    _supabase_rest_url = None
    _http_client = None
    load_news_df = None


ANNUAL_REPORT_BUCKET = "annual-reports"
SUPPORTED_FILE_EXTENSIONS = {".pdf", ".zip", ".xhtml", ".html", ".htm", ".xml", ".xbrl", ".json"}
REPORT_FILE_EXTENSIONS = {".pdf", ".xhtml", ".html", ".htm", ".xml", ".xbrl"}

OFFICIAL_SEGMENT_TOKENS = (
    "official",
    "oficial",
    "baltic main list",
    "main list",
    "baltijos oficialusis",
    "oficialusis",
)

SECONDARY_SEGMENT_TOKENS = (
    "secondary",
    "papildom",
    "baltic secondary list",
    "baltijos papildomasis",
    "papildomasis",
)

ANNUAL_CATEGORY_TOKENS = (
    "metinė informacija",
    "metine informacija",
    "annual information",
)

ANNUAL_TITLE_TOKENS = (
    "metinė ataskaita",
    "metine ataskaita",
    "metinis pranešimas",
    "metinis pranesimas",
    "audituota metinė",
    "audituota metine",
    "annual report",
    "audited annual",
    "annual information",
)

NON_ANNUAL_TITLE_TOKENS = (
    "3 mėn",
    "3 men",
    "trijų mėnesių",
    "triju menesiu",
    "6 mėn",
    "6 men",
    "šešių mėnesių",
    "sesiu menesiu",
    "9 mėn",
    "9 men",
    "devynių mėnesių",
    "devyniu menesiu",
    "tarpinė",
    "tarpine",
    "interim",
    "preliminar",
    "prognoz",
    "dividend",
)


def _notify(progress: Optional[Callable[[str], None]], message: str) -> None:
    if progress:
        progress(message)


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_lithuanian(value: str) -> str:
    s = str(value or "").lower().strip()
    repl = str.maketrans({
        "ą": "a", "č": "c", "ę": "e", "ė": "e", "į": "i",
        "š": "s", "ų": "u", "ū": "u", "ž": "z",
    })
    s = s.translate(repl)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _issuer_norm_key(value: str) -> str:
    s = _norm_lithuanian(value)
    s = re.sub(r"\b(ab|uab|as|asa|akcine bendrove|uzdaroji akcine bendrove)\b", " ", s)
    s = s.replace(" group", " ").replace(" grupe", " ")
    return re.sub(r"\s+", " ", s).strip()


def _segment_is_official_or_secondary(segment: str) -> bool:
    seg = _norm_lithuanian(segment)
    if not seg:
        return False
    return any(_norm_lithuanian(t) in seg for t in OFFICIAL_SEGMENT_TOKENS + SECONDARY_SEGMENT_TOKENS)


def _require_supabase() -> None:
    if _supabase_headers is None or _supabase_rest_url is None or _http_client is None:
        raise RuntimeError("Nerastas supabase_cache.py arba jo _supabase_headers/_supabase_rest_url/_http_client funkcijos.")


def _headers() -> Dict[str, str]:
    _require_supabase()
    return _supabase_headers()


def _rest_url(table: str) -> str:
    _require_supabase()
    return _supabase_rest_url(table)


def _client():
    _require_supabase()
    return _http_client()


def _storage_base_url() -> str:
    if _supabase_rest_url is not None:
        rest = _supabase_rest_url("__dummy__")
        return rest.split("/rest/v1/")[0].rstrip("/")
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("Nepavyko nustatyti Supabase URL.")
    return url


def load_target_issuers() -> pd.DataFrame:
    """Grąžina tik VLN oficialiojo ir papildomojo sąrašo emitentus iš market_issuers."""
    url = _rest_url("market_issuers")
    params = {
        "select": "issuer,issuer_norm,company,company_norm,ticker,segment,market,unique_key,last_seen_date",
        "market": "eq.VLN",
        "order": "issuer.asc",
        "limit": "5000",
    }
    with _client() as client:
        resp = client.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
        rows = resp.json() or []

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["issuer", "issuer_norm", "company", "company_norm", "ticker", "segment", "market"])

    for col in ["issuer", "issuer_norm", "company", "company_norm", "ticker", "segment", "market"]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str).str.strip()

    df["issuer"] = df.apply(lambda r: r.get("issuer") or r.get("company") or "", axis=1)
    df["issuer_norm"] = df.apply(
        lambda r: r.get("issuer_norm") or r.get("company_norm") or _issuer_norm_key(r.get("issuer") or r.get("company")),
        axis=1,
    )
    df = df[df["segment"].apply(_segment_is_official_or_secondary)].copy()
    df = df[df["issuer"].astype(str).str.strip().ne("")].copy()
    return df.reset_index(drop=True)


def _issuer_lookup(issuers_df: pd.DataFrame) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    if issuers_df is None or issuers_df.empty:
        return lookup
    for _, row in issuers_df.iterrows():
        canonical = str(row.get("issuer") or row.get("company") or "").strip()
        if not canonical:
            continue
        candidates = [
            row.get("issuer"), row.get("company"), row.get("issuer_norm"),
            row.get("company_norm"), row.get("ticker"), canonical,
        ]
        for c in candidates:
            key = _issuer_norm_key(c)
            if key:
                lookup[key] = canonical
    return lookup


def _canonical_from_text(value: str, lookup: Dict[str, str]) -> str:
    key = _issuer_norm_key(value)
    if key in lookup:
        return lookup[key]
    # Griežtesnė paieška pagal pilną normalizuotą pavadinimą tekste.
    text_key = _issuer_norm_key(value)
    best = ""
    best_len = 0
    for k, canonical in lookup.items():
        if not k or len(k) < 3:
            continue
        if re.search(rf"(?:^|\s){re.escape(k)}(?:\s|$)", text_key):
            if len(k) > best_len:
                best = canonical
                best_len = len(k)
    return best


def _is_annual_news(row: dict) -> bool:
    category = _norm_lithuanian(row.get("category", ""))
    title = _norm_lithuanian(row.get("title", ""))
    content = _norm_lithuanian(str(row.get("content", ""))[:1000])
    text = f"{category} {title} {content}"

    category_ok = any(_norm_lithuanian(t) in category for t in ANNUAL_CATEGORY_TOKENS)
    title_ok = any(_norm_lithuanian(t) in text for t in ANNUAL_TITLE_TOKENS)
    non_annual = any(_norm_lithuanian(t) in text for t in NON_ANNUAL_TITLE_TOKENS)

    if category_ok and title_ok and not non_annual:
        return True
    if category_ok and ("annual" in text or "metin" in text or "audituot" in text) and not non_annual:
        return True
    return False


def _load_annual_news(start_date: date, end_date: date, issuers_df: pd.DataFrame) -> pd.DataFrame:
    """Paima CRIB metinės informacijos naujienas iš market_news per load_news_df arba REST fallback."""
    if load_news_df is not None:
        df = load_news_df("crib", start_date, end_date)
    else:
        start_iso = f"{start_date}T00:00:00"
        end_iso = f"{end_date}T23:59:59"
        url = _rest_url("market_news")
        params = {
            "select": "*",
            "source": "eq.crib",
            "published_at": [f"gte.{start_iso}", f"lte.{end_iso}"],
            "order": "published_at.desc",
            "limit": "5000",
        }
        with _client() as client:
            resp = client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            df = pd.DataFrame(resp.json() or [])

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    for col in ["company", "category", "title", "published_at", "url", "content"]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str).str.strip()

    lookup = _issuer_lookup(issuers_df)
    df["issuer"] = df.apply(
        lambda r: _canonical_from_text(r.get("company", ""), lookup) or _canonical_from_text(f"{r.get('title', '')} {r.get('content', '')}", lookup),
        axis=1,
    )
    df["issuer_norm"] = df["issuer"].apply(_issuer_norm_key)
    df["crib_url"] = df["url"].fillna("").astype(str).str.strip()
    df["published_at_dt"] = pd.to_datetime(df["published_at"], errors="coerce", utc=True)
    df["report_year"] = df["published_at_dt"].dt.year
    df = df[df.apply(_is_annual_news, axis=1)].copy()
    df = df[df["issuer"].astype(str).str.strip().ne("")].copy()
    df = df[df["crib_url"].astype(str).str.strip().ne("")].copy()
    df = df.drop_duplicates(subset=["crib_url"], keep="first")
    return df.reset_index(drop=True)


def _guess_report_year(row: dict) -> Optional[int]:
    title = str(row.get("title") or row.get("crib_title") or "")
    content = str(row.get("content") or "")[:1000]
    published_at = row.get("published_at") or row.get("published_at_dt")

    text = f"{title} {content}"
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text)]
    if years:
        # Metinė ataskaita paprastai yra už praėjusius metus; imame mažiausią realistišką paminėtą metus.
        plausible = [y for y in years if 2018 <= y <= date.today().year]
        if plausible:
            return min(plausible)

    try:
        dt = pd.to_datetime(published_at, errors="coerce")
        if pd.notna(dt):
            return int(dt.year) - 1
    except Exception:
        pass
    return None


def _extract_links_from_html(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = (a.get_text(" ", strip=True) or "").lower()
        full = urljoin(base_url, href)
        ext = os.path.splitext(urlparse(full).path.lower())[1]
        href_l = href.lower()
        if (
            ext in SUPPORTED_FILE_EXTENSIONS
            or "viewattachment.action" in href_l
            or "attachment" in href_l
            or "download" in href_l
            or "pdf" in text
            or "xbrl" in text
            or "xhtml" in text
            or "zip" in text
        ):
            if full not in links:
                links.append(full)
    return _rank_attachment_links(links)


def _rank_attachment_links(links: Iterable[str]) -> List[str]:
    seen = set()
    clean: List[str] = []
    for link in links or []:
        if not link or link in seen:
            continue
        seen.add(link)
        clean.append(link)

    def score(u: str) -> int:
        ul = u.lower()
        if "crib.lt/cns-web/oam/viewattachment" in ul:
            return 0
        if "viewattachment.action" in ul:
            return 1
        if ul.endswith(".zip") or "zip" in ul:
            return 2
        if ul.endswith(".xhtml") or ul.endswith(".xbrl") or ul.endswith(".xml"):
            return 3
        if ul.endswith(".pdf") or "pdf" in ul:
            return 4
        if "globenewswire.com/resource/download" in ul:
            return 5
        return 9

    return sorted(clean, key=score)


def extract_attachment_links(crib_url: str) -> List[str]:
    if not crib_url:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(crib_url, headers=headers, timeout=30, verify=False)
    resp.raise_for_status()
    return _extract_links_from_html(resp.text, crib_url)


def _safe_filename_from_response(url: str, resp: requests.Response) -> str:
    cd = resp.headers.get("content-disposition") or resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, flags=re.I)
    if m:
        return os.path.basename(urlparse(requests.utils.unquote(m.group(1))).path) or "attachment"
    m = re.search(r'filename="?([^";]+)"?', cd, flags=re.I)
    if m:
        return os.path.basename(m.group(1)) or "attachment"
    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    if name and "." in name:
        return name
    # CRIB attachment URL dažnai neturi normalaus failo vardo.
    ext = mimetypes.guess_extension(resp.headers.get("content-type", "").split(";")[0].strip()) or ""
    if not ext and resp.content[:4] == b"%PDF":
        ext = ".pdf"
    if not ext and resp.content[:2] == b"PK":
        ext = ".zip"
    if not ext:
        ext = ".bin"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"attachment_{digest}{ext}"


def download_attachment(url: str) -> Tuple[bytes, str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/pdf,application/zip,text/html,application/xml,*/*",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(url, headers=headers, timeout=60, verify=False, allow_redirects=True)
    resp.raise_for_status()
    content = resp.content or b""
    file_name = _safe_filename_from_response(url, resp)
    content_type = resp.headers.get("content-type", "").split(";")[0].strip() or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    return content, file_name, content_type


def _file_type(file_name: str, content: bytes, content_type: str = "") -> str:
    name = (file_name or "").lower()
    ext = os.path.splitext(name)[1]
    ctype = (content_type or "").lower()
    if content[:2] == b"PK" or ext == ".zip" or "zip" in ctype:
        return "zip"
    if content[:4] == b"%PDF" or ext == ".pdf" or "pdf" in ctype:
        return "pdf"
    if ext in {".xhtml", ".html", ".htm"}:
        return "xhtml"
    if ext in {".xbrl"} or "xbrl" in name:
        return "xbrl"
    if ext == ".xml" or "xml" in ctype:
        return "xml"
    return ext.replace(".", "") or "unknown"


def _clean_storage_part(value: str) -> str:
    s = _norm_lithuanian(value)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:90] or "unknown"


def _storage_path(issuer: str, report_year: Optional[int], file_name: str, suffix: str = "") -> str:
    safe_issuer = _clean_storage_part(issuer)
    year = str(report_year or "unknown")
    base = os.path.basename(file_name or "attachment.bin")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "attachment.bin"
    if suffix:
        stem, ext = os.path.splitext(base)
        base = f"{stem}_{suffix}{ext}"
    return f"{safe_issuer}/{year}/{base}"


def upload_to_storage(storage_path: str, content: bytes, content_type: str) -> Tuple[bool, str]:
    base = _storage_base_url()
    path = quote(storage_path, safe="/")
    url = f"{base}/storage/v1/object/{ANNUAL_REPORT_BUCKET}/{path}"
    headers = dict(_headers())
    headers["Content-Type"] = content_type or "application/octet-stream"
    headers["x-upsert"] = "true"
    with _client() as client:
        resp = client.post(url, headers=headers, content=content)
        if resp.status_code in (200, 201):
            return True, ""
        if resp.status_code == 409:
            # Jei jau yra, laikome sėkme.
            return True, "already_exists"
        return False, f"Supabase Storage upload klaida: {resp.status_code} - {resp.text[:500]}"


def _extract_pdf_text(content: bytes) -> str:
    if not content or pdfplumber is None or not content[:20].lstrip().startswith(b"%PDF"):
        return ""
    texts: List[str] = []
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            for page in pdf.pages:
                try:
                    txt = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
                except Exception:
                    txt = page.extract_text() or ""
                if txt:
                    texts.append(txt)
    except Exception:
        return ""
    return "\n".join(texts).strip()[:200000]


def upsert_annual_report(row: dict) -> Optional[int]:
    url = _rest_url("annual_reports")
    report_year = row.get("report_year") or _guess_report_year(row)
    payload = {
        "issuer": row.get("issuer") or "",
        "issuer_norm": row.get("issuer_norm") or _issuer_norm_key(row.get("issuer") or ""),
        "company": row.get("company") or row.get("issuer") or "",
        "market": "VLN",
        "report_year": report_year,
        "report_type": "Metinė",
        "crib_url": row.get("crib_url") or row.get("url") or "",
        "crib_title": row.get("title") or "",
        "crib_category": row.get("category") or "",
        "published_at": _to_iso(row.get("published_at") or row.get("published_at_dt")),
        "parse_status": "report_found",
        "updated_at": datetime.utcnow().isoformat(),
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    with _client() as client:
        resp = client.post(
            url,
            headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            params={"on_conflict": "crib_url"},
            json=payload,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Supabase UPSERT klaida annual_reports: {resp.status_code} - {resp.text}")
        data = resp.json() or []
        if data:
            return int(data[0]["id"])

    # Fallback jei return=representation neveiktų.
    params = {"select": "id", "crib_url": f"eq.{payload['crib_url']}", "limit": "1"}
    with _client() as client:
        resp = client.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
        data = resp.json() or []
        return int(data[0]["id"]) if data else None


def _to_iso(value) -> Optional[str]:
    try:
        if value is not None and not pd.isna(value):
            return pd.to_datetime(value).isoformat()
    except Exception:
        pass
    return None


def update_report_status(report_id: int, status: str) -> None:
    url = _rest_url("annual_reports")
    with _client() as client:
        client.patch(
            url,
            headers={**_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{report_id}"},
            json={"parse_status": status, "updated_at": datetime.utcnow().isoformat()},
        )


def upsert_report_file(payload: dict) -> Optional[int]:
    url = _rest_url("annual_report_files")
    with _client() as client:
        resp = client.post(
            url,
            headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            params={"on_conflict": "file_url"},
            json={k: v for k, v in payload.items() if v is not None},
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Supabase UPSERT klaida annual_report_files: {resp.status_code} - {resp.text}")
        data = resp.json() or []
        return int(data[0]["id"]) if data else None


def _iter_zip_files(zip_content: bytes) -> Iterable[Tuple[str, bytes]]:
    with zipfile.ZipFile(BytesIO(zip_content)) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            ext = os.path.splitext(name.lower())[1]
            if ext not in REPORT_FILE_EXTENSIONS:
                continue
            try:
                yield name, zf.read(name)
            except Exception:
                continue


def _save_single_file(
    annual_report_id: int,
    issuer: str,
    issuer_norm: str,
    report_year: Optional[int],
    file_url: str,
    file_name: str,
    content: bytes,
    content_type: str,
    parent_zip_name: str = "",
) -> Dict[str, object]:
    ftype = _file_type(file_name, content, content_type)
    suffix = ""
    if parent_zip_name:
        suffix = hashlib.sha1(f"{parent_zip_name}/{file_name}".encode("utf-8")).hexdigest()[:8]
    storage_path = _storage_path(issuer, report_year, file_name, suffix=suffix)
    uploaded, upload_note = upload_to_storage(storage_path, content, content_type)
    raw_text = _extract_pdf_text(content) if ftype == "pdf" else ""
    status = "saved"
    if not uploaded:
        status = "storage_error"

    payload = {
        "annual_report_id": annual_report_id,
        "issuer": issuer,
        "issuer_norm": issuer_norm,
        "report_year": report_year,
        "file_url": file_url,
        "file_name": file_name,
        "file_type": ftype,
        "storage_bucket": ANNUAL_REPORT_BUCKET,
        "storage_path": storage_path if uploaded else "",
        "content_type": content_type,
        "file_size": len(content or b""),
        "raw_text": raw_text,
        "parse_status": status if uploaded else upload_note[:1000],
    }
    file_id = upsert_report_file(payload)
    return {"file_id": file_id, "file_name": file_name, "file_type": ftype, "status": status, "note": upload_note}


def process_annual_report_notice(row: dict, progress: Optional[Callable[[str], None]] = None) -> Dict[str, object]:
    issuer = str(row.get("issuer") or "").strip()
    issuer_norm = str(row.get("issuer_norm") or _issuer_norm_key(issuer)).strip()
    crib_url = str(row.get("crib_url") or row.get("url") or "").strip()
    report_year = row.get("report_year") or _guess_report_year(row)

    result = {
        "issuer": issuer,
        "title": row.get("title", ""),
        "crib_url": crib_url,
        "report_id": None,
        "attachments_found": 0,
        "files_saved": 0,
        "zip_inner_saved": 0,
        "errors": [],
    }

    report_id = upsert_annual_report({**row, "report_year": report_year})
    result["report_id"] = report_id
    if not report_id:
        result["errors"].append("Nepavyko sukurti annual_reports įrašo.")
        return result

    try:
        links = extract_attachment_links(crib_url)
    except Exception as exc:
        update_report_status(report_id, "attachment_links_error")
        result["errors"].append(f"Nepavyko nuskaityti CRIB priedų: {exc}")
        return result

    result["attachments_found"] = len(links)
    if not links:
        update_report_status(report_id, "no_attachments")
        return result

    for link in links:
        _notify(progress, f"Atsisiunčiamas priedas: {issuer} | {link}")
        try:
            content, file_name, content_type = download_attachment(link)
            if not content:
                result["errors"].append(f"Tuščias failas: {link}")
                continue

            saved = _save_single_file(
                annual_report_id=report_id,
                issuer=issuer,
                issuer_norm=issuer_norm,
                report_year=report_year,
                file_url=link,
                file_name=file_name,
                content=content,
                content_type=content_type,
            )
            if saved.get("status") == "saved":
                result["files_saved"] += 1
            else:
                result["errors"].append(str(saved.get("note") or "Storage klaida"))

            if saved.get("file_type") == "zip":
                for inner_name, inner_content in _iter_zip_files(content):
                    inner_type = mimetypes.guess_type(inner_name)[0] or "application/octet-stream"
                    inner_url = f"{link}#/{inner_name}"
                    inner_saved = _save_single_file(
                        annual_report_id=report_id,
                        issuer=issuer,
                        issuer_norm=issuer_norm,
                        report_year=report_year,
                        file_url=inner_url,
                        file_name=os.path.basename(inner_name),
                        content=inner_content,
                        content_type=inner_type,
                        parent_zip_name=file_name,
                    )
                    if inner_saved.get("status") == "saved":
                        result["zip_inner_saved"] += 1
                    else:
                        result["errors"].append(str(inner_saved.get("note") or "ZIP vidinio failo Storage klaida"))

        except Exception as exc:
            result["errors"].append(f"Failo apdorojimo klaida {link}: {exc}")
            continue

    if result["files_saved"] or result["zip_inner_saved"]:
        update_report_status(report_id, "files_saved")
    elif result["errors"]:
        update_report_status(report_id, "files_error")
    else:
        update_report_status(report_id, "no_supported_files")
    return result


def load_annual_reports_for_period(start_date: date, end_date: date) -> pd.DataFrame:
    issuers = load_target_issuers()
    return _load_annual_news(start_date, end_date, issuers)


def update_annual_reports_for_period(start_date: date, end_date: date, progress: Optional[Callable[[str], None]] = None) -> Dict[str, object]:
    stats: Dict[str, object] = {
        "target_issuers": 0,
        "annual_notices_found": 0,
        "reports_processed": 0,
        "reports_saved": 0,
        "attachments_found": 0,
        "files_saved": 0,
        "zip_inner_saved": 0,
        "errors": [],
        "details": [],
    }

    issuers = load_target_issuers()
    stats["target_issuers"] = len(issuers)
    if issuers.empty:
        stats["errors"].append("market_issuers lentelėje nerasta VLN oficialiojo / papildomojo sąrašo emitentų.")
        return stats

    news = _load_annual_news(start_date, end_date, issuers)
    stats["annual_notices_found"] = len(news)
    if news.empty:
        return stats

    for _, r in news.iterrows():
        row = r.to_dict()
        _notify(progress, f"Tikrinama: {row.get('issuer', '')} | {row.get('title', '')}")
        try:
            detail = process_annual_report_notice(row, progress=progress)
            stats["reports_processed"] = int(stats["reports_processed"]) + 1
            if detail.get("report_id"):
                stats["reports_saved"] = int(stats["reports_saved"]) + 1
            stats["attachments_found"] = int(stats["attachments_found"]) + int(detail.get("attachments_found") or 0)
            stats["files_saved"] = int(stats["files_saved"]) + int(detail.get("files_saved") or 0)
            stats["zip_inner_saved"] = int(stats["zip_inner_saved"]) + int(detail.get("zip_inner_saved") or 0)
            if detail.get("errors"):
                stats["errors"].extend(detail.get("errors") or [])
            stats["details"].append(detail)
        except Exception as exc:
            stats["errors"].append(f"{row.get('issuer', '')}: {exc}")
            continue

    return stats


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Atsisiųsti ir išsaugoti CRIB metines ataskaitas į Supabase.")
    parser.add_argument("--start", type=str, default=f"{date.today().year - 2}-01-01", help="Pradžios data YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=date.today().isoformat(), help="Pabaigos data YYYY-MM-DD")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    start = pd.to_datetime(args.start).date()
    end = pd.to_datetime(args.end).date()

    def progress(msg: str) -> None:
        print(msg, flush=True)

    stats = update_annual_reports_for_period(start, end, progress=progress)
    print("\nBAIGTA")
    for key in ["target_issuers", "annual_notices_found", "reports_processed", "reports_saved", "attachments_found", "files_saved", "zip_inner_saved"]:
        print(f"{key}: {stats.get(key)}")
    if stats.get("errors"):
        print("\nKLAIDOS:")
        for err in list(stats.get("errors") or [])[:50]:
            print(f"- {err}")


if __name__ == "__main__":
    main()

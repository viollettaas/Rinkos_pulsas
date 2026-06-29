# -*- coding: utf-8 -*-
"""
Metiniu ataskaitu modulis Rinkos pulsui.

Funkcionalumas:
- rodo laikotarpio pasirinkima Streamlit puslapyje;
- is market_news paima CRIB kategorijos "Metine informacija" / "Annual information" irasus;
- filtruoja tik market_issuers lenteles VLN emitentus;
- suranda PDF / XBRL / XML / XHTML priedus CRIB puslapyje;
- issaugo annual_reports ir annual_report_files lentelese;
- ikelia pacius failus i Supabase Storage bucket annual-reports;
- PDF tekstui bando issaugoti raw_text ateities analizei;
- klaidu nebeslepia: puslapyje parodo, kodel konkretus irasas nebuvo issaugotas.
"""

import os
import re
import mimetypes
import traceback
from datetime import date, timedelta
from io import BytesIO
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    import pdfplumber
except Exception:
    pdfplumber = None

from supabase_cache import load_news_df

try:
    from supabase_cache import _supabase_headers, _supabase_rest_url, _http_client
except Exception:
    _supabase_headers = None
    _supabase_rest_url = None
    _http_client = None


ANNUAL_CATEGORY_PATTERNS = (
    "metinė informacija",
    "metine informacija",
    "annual information",
)

REPORT_FILE_EXTENSIONS = (".pdf", ".xml", ".xbrl", ".xhtml", ".html", ".zip")
REPORT_BUCKET = "annual-reports"


# ============================================================
# Bendros pagalbines funkcijos
# ============================================================


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _lt_norm(value) -> str:
    s = str(value or "").lower().strip()
    repl = str.maketrans({
        "ą": "a", "č": "c", "ę": "e", "ė": "e", "į": "i",
        "š": "s", "ų": "u", "ū": "u", "ž": "z",
    })
    s = s.translate(repl)
    s = re.sub(r"\b(ab|uab|as|asa|akcine bendrove|uzdaroji akcine bendrove)\b", " ", s)
    s = s.replace(" group", " ").replace(" grupe", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_annual_category(category: str) -> bool:
    c = str(category or "").lower()
    c_norm = _lt_norm(c)
    return any(_lt_norm(p) in c_norm for p in ANNUAL_CATEGORY_PATTERNS)


def _is_annual_report_title(title: str, category: str = "") -> bool:
    txt = _lt_norm(f"{category} {title}")
    if any(x in txt for x in ["preliminar", "dividend", "prognoz", "presentation", "prezentacij"]):
        return False
    return (
        _is_annual_category(category)
        or "metin" in txt
        or "annual report" in txt
        or "annual information" in txt
        or "audituot" in txt
        or "audited" in txt
    )


def _safe_iso_ts(value):
    try:
        if value is not None and not pd.isna(value):
            return pd.to_datetime(value).isoformat()
    except Exception:
        pass
    return None


def _safe_date(value):
    try:
        if value is not None and not pd.isna(value):
            return pd.to_datetime(value).date()
    except Exception:
        pass
    return None


def _detect_report_year(title: str, published_at=None) -> Optional[int]:
    title = str(title or "")
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", title)]
    if years:
        return max(years)
    d = _safe_date(published_at)
    if d:
        # Metines ataskaitos dazniausiai skelbiamos kitais metais uz praeitus finansinius metus.
        return d.year - 1
    return None


def _guess_file_type(url: str, content_type: str = "", name: str = "") -> str:
    u = f"{url or ''} {name or ''} {content_type or ''}".lower()
    if ".pdf" in u or "application/pdf" in u:
        return "pdf"
    if ".xbrl" in u or "xbrl" in u:
        return "xbrl"
    if ".xhtml" in u:
        return "xhtml"
    if ".xml" in u:
        return "xml"
    if ".zip" in u:
        return "zip"
    if ".html" in u or "text/html" in u:
        return "html"
    return "file"


def _filename_from_url(url: str, fallback: str = "report") -> str:
    try:
        parsed = urlparse(url)
        name = os.path.basename(parsed.path)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
        return name or fallback
    except Exception:
        return fallback


# ============================================================
# Supabase pagalbininkai
# ============================================================


def _require_supabase_helpers():
    if _supabase_headers is None or _supabase_rest_url is None or _http_client is None:
        raise RuntimeError("Nepavyko importuoti Supabase pagalbiniu funkciju is supabase_cache.py")


def _headers_json(prefer: Optional[str] = None) -> Dict[str, str]:
    _require_supabase_helpers()
    h = dict(_supabase_headers())
    h["Content-Type"] = "application/json"
    if prefer:
        h["Prefer"] = prefer
    return h


def _supabase_base_url() -> str:
    env_url = os.getenv("SUPABASE_URL") or os.getenv("supabase_url")
    if env_url:
        return env_url.rstrip("/")
    # Is REST URL: https://xxx.supabase.co/rest/v1/table -> https://xxx.supabase.co
    test_url = _supabase_rest_url("__dummy__")
    return test_url.split("/rest/v1/")[0].rstrip("/")


def _get_table_rows(table: str, params: Dict) -> List[dict]:
    _require_supabase_helpers()
    url = _supabase_rest_url(table)
    with _http_client() as client:
        resp = client.get(url, headers=_supabase_headers(), params=params)
        resp.raise_for_status()
        return resp.json() or []


def _post_table_row(table: str, row: dict, on_conflict: Optional[str] = None) -> dict:
    _require_supabase_helpers()
    url = _supabase_rest_url(table)
    params = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    headers = _headers_json("resolution=merge-duplicates,return=representation")
    with _http_client() as client:
        resp = client.post(url, headers=headers, params=params, json=row)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Supabase INSERT/UPSERT klaida {table}: {resp.status_code} - {resp.text}")
        data = resp.json() or []
        return data[0] if isinstance(data, list) and data else {}


def _patch_table_rows(table: str, filters: Dict[str, str], row: dict) -> bool:
    _require_supabase_helpers()
    url = _supabase_rest_url(table)
    with _http_client() as client:
        resp = client.patch(url, headers=_headers_json("return=minimal"), params=filters, json=row)
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"Supabase UPDATE klaida {table}: {resp.status_code} - {resp.text}")
        return True


def _upload_to_storage(bucket: str, storage_path: str, content: bytes, content_type: str) -> bool:
    base = _supabase_base_url()
    storage_path = storage_path.lstrip("/")
    url = f"{base}/storage/v1/object/{bucket}/{storage_path}"
    headers = dict(_supabase_headers())
    headers["Content-Type"] = content_type or "application/octet-stream"
    headers["x-upsert"] = "true"
    resp = requests.post(url, headers=headers, data=content, timeout=90)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Supabase Storage upload klaida: {resp.status_code} - {resp.text[:500]}")
    return True


def load_vln_issuers() -> pd.DataFrame:
    rows = _get_table_rows(
        "market_issuers",
        {
            "select": "issuer,company,issuer_norm,company_norm,ticker,market",
            "market": "eq.VLN",
            "order": "issuer.asc",
        },
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["issuer", "issuer_norm", "company", "ticker", "market"])
    for col in ["issuer", "company", "issuer_norm", "company_norm", "ticker", "market"]:
        if col not in df.columns:
            df[col] = ""
    df["issuer"] = df["issuer"].fillna(df["company"]).fillna("").astype(str).str.strip()
    df["issuer_norm_calc"] = df["issuer"].apply(_lt_norm)
    return df


def _build_issuer_lookup(issuers_df: pd.DataFrame) -> Dict[str, str]:
    lookup = {}
    if issuers_df is None or issuers_df.empty:
        return lookup
    for _, r in issuers_df.iterrows():
        canonical = str(r.get("issuer") or r.get("company") or "").strip()
        if not canonical:
            continue
        for c in [canonical, r.get("company"), r.get("issuer_norm"), r.get("company_norm"), r.get("ticker")]:
            key = _lt_norm(c)
            if key:
                lookup[key] = canonical
    return lookup


def _infer_issuer_from_text(text: str, lookup: Dict[str, str]) -> str:
    txt = _lt_norm(text)
    best = ""
    best_len = 0
    for key, issuer in lookup.items():
        if not key or len(key) < 3:
            continue
        if re.search(rf"(?:^|\s){re.escape(key)}(?:\s|$)", txt):
            if len(key) > best_len:
                best = issuer
                best_len = len(key)
    return best


# ============================================================
# CRIB / failu nuskaitymas
# ============================================================


def load_annual_crib_news(start_date, end_date, issuers_df: pd.DataFrame) -> pd.DataFrame:
    df = load_news_df("crib", start_date, end_date)
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    for col in ["company", "category", "title", "published_at", "url", "content"]:
        if col not in df.columns:
            df[col] = ""

    df["category"] = df["category"].fillna("").astype(str).str.strip()
    df["title"] = df["title"].fillna("").astype(str).str.strip()
    df["content"] = df["content"].fillna("").astype(str).str.strip()
    df["crib_url"] = df["url"].fillna("").astype(str).str.strip()
    df["published_at_dt"] = pd.to_datetime(df["published_at"], errors="coerce", utc=False)

    df = df[df.apply(lambda r: _is_annual_report_title(r.get("title", ""), r.get("category", "")), axis=1)].copy()
    if df.empty:
        return df

    lookup = _build_issuer_lookup(issuers_df)
    allowed = set(issuers_df["issuer"].dropna().astype(str).str.strip()) if issuers_df is not None and not issuers_df.empty else set()

    df["issuer"] = df["company"].fillna("").astype(str).str.strip()
    missing = df["issuer"].eq("") | (~df["issuer"].isin(allowed))
    if missing.any():
        df.loc[missing, "issuer"] = df.loc[missing].apply(
            lambda r: _infer_issuer_from_text(f"{r.get('title','')} {r.get('content','')}", lookup),
            axis=1,
        )

    df = df[df["issuer"].isin(allowed)].copy()
    if df.empty:
        return df

    df["issuer_norm"] = df["issuer"].apply(_lt_norm)
    df["report_year"] = df.apply(lambda r: _detect_report_year(r.get("title", ""), r.get("published_at")), axis=1)
    df = df[df["crib_url"].ne("")].copy()
    df = df.drop_duplicates(subset=["crib_url"], keep="first")
    return df.reset_index(drop=True)


def _extract_file_links_from_html(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = (a.get_text(" ", strip=True) or "").lower()
        href_l = href.lower()
        is_file = any(ext in href_l for ext in REPORT_FILE_EXTENSIONS)
        is_attachment = "viewattachment.action" in href_l or "attachment" in href_l or "download" in href_l
        text_hint = any(x in text for x in ["pdf", "xbrl", "xml", "metin", "annual", "ataskait"])
        if is_file or is_attachment or text_hint:
            full = urljoin(base_url, href)
            if full not in links:
                links.append(full)

    def score(u: str) -> int:
        ul = u.lower()
        if "crib.lt/cns-web/oam/viewattachment" in ul:
            return 0
        if "viewattachment.action" in ul:
            return 1
        if any(ext in ul for ext in [".xbrl", ".xml", ".xhtml"]):
            return 2
        if ".pdf" in ul:
            return 3
        if "globenewswire.com/resource/download" in ul:
            return 9
        return 5

    return sorted(links, key=score)


def extract_report_file_links(crib_url: str) -> List[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(crib_url, headers=headers, verify=False, timeout=45)
    resp.raise_for_status()
    return _extract_file_links_from_html(resp.text, crib_url)


def download_file(file_url: str) -> Tuple[bytes, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/pdf,application/xml,text/xml,application/xhtml+xml,text/html,application/zip,application/octet-stream,*/*",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(file_url, headers=headers, verify=False, timeout=90, allow_redirects=True)
    resp.raise_for_status()
    content = resp.content or b""
    content_type = resp.headers.get("Content-Type", "") or mimetypes.guess_type(file_url)[0] or "application/octet-stream"
    return content, content_type


def extract_pdf_text(content: bytes) -> str:
    if not content or not content[:20].lstrip().startswith(b"%PDF"):
        return ""
    if pdfplumber is None:
        return ""
    texts = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for page in pdf.pages:
            try:
                txt = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            except Exception:
                txt = page.extract_text() or ""
            if txt:
                texts.append(txt)
    return "\n".join(texts).strip()


# ============================================================
# Saugojimo logika
# ============================================================


def save_annual_report_notice(row: dict) -> dict:
    report_row = {
        "issuer": str(row.get("issuer") or "").strip(),
        "issuer_norm": _lt_norm(row.get("issuer") or ""),
        "company": str(row.get("company") or row.get("issuer") or "").strip(),
        "market": "VLN",
        "report_year": int(row.get("report_year")) if pd.notna(row.get("report_year")) else None,
        "report_type": "Metinė",
        "crib_url": str(row.get("crib_url") or "").strip(),
        "crib_title": str(row.get("title") or "").strip(),
        "crib_category": str(row.get("category") or "").strip(),
        "published_at": _safe_iso_ts(row.get("published_at")),
        "parse_status": "notice_saved",
    }
    return _post_table_row("annual_reports", report_row, on_conflict="crib_url")


def save_annual_report_file(report_id: int, notice_row: dict, file_url: str, content: bytes, content_type: str) -> dict:
    issuer = str(notice_row.get("issuer") or "").strip()
    report_year = int(notice_row.get("report_year")) if pd.notna(notice_row.get("report_year")) else None
    safe_issuer = _lt_norm(issuer).replace(" ", "_") or "issuer"
    file_name = _filename_from_url(file_url, fallback=f"annual_report_{report_id}")
    file_type = _guess_file_type(file_url, content_type, file_name)
    storage_path = f"{report_year or 'unknown'}/{safe_issuer}/{report_id}_{file_name}"

    storage_status = "not_uploaded"
    try:
        if content:
            _upload_to_storage(REPORT_BUCKET, storage_path, content, content_type)
            storage_status = "uploaded"
    except Exception as exc:
        storage_status = f"storage_error: {str(exc)[:300]}"

    raw_text = ""
    if file_type == "pdf" and content:
        try:
            raw_text = extract_pdf_text(content)[:200000]
        except Exception as exc:
            raw_text = ""
            if storage_status == "uploaded":
                storage_status = f"uploaded_pdf_text_error: {str(exc)[:200]}"

    file_row = {
        "annual_report_id": report_id,
        "issuer": issuer,
        "report_year": report_year,
        "file_url": file_url,
        "file_name": file_name,
        "file_type": file_type,
        "storage_bucket": REPORT_BUCKET,
        "storage_path": storage_path if storage_status.startswith("uploaded") else "",
        "content_type": content_type,
        "file_size": len(content or b""),
        "raw_text": raw_text,
        "parse_status": storage_status,
    }
    return _post_table_row("annual_report_files", file_row, on_conflict="file_url")


def update_annual_reports_for_period(start_date, end_date, progress=None) -> dict:
    stats = {
        "issuers_loaded": 0,
        "annual_notices_found": 0,
        "reports_saved": 0,
        "files_found": 0,
        "files_saved": 0,
        "errors": 0,
        "error_rows": [],
    }

    issuers_df = load_vln_issuers()
    stats["issuers_loaded"] = len(issuers_df)
    if issuers_df.empty:
        raise RuntimeError("market_issuers lenteleje nerasta VLN emitentu.")

    notices = load_annual_crib_news(start_date, end_date, issuers_df)
    stats["annual_notices_found"] = len(notices)
    if notices.empty:
        return stats

    for _, r in notices.iterrows():
        title = str(r.get("title") or "")
        crib_url = str(r.get("crib_url") or "")
        issuer = str(r.get("issuer") or "")
        if progress:
            progress(f"Tikrinama: {issuer} | {title[:100]}")

        try:
            report = save_annual_report_notice(r.to_dict())
            report_id = report.get("id")
            if not report_id:
                # Jei REST negrąžino id del konflikto, pasiimame pagal crib_url.
                found = _get_table_rows("annual_reports", {"select": "id", "crib_url": f"eq.{crib_url}", "limit": "1"})
                report_id = found[0].get("id") if found else None
            if not report_id:
                raise RuntimeError("annual_reports irasas issaugotas be id arba nepavyko jo rasti pagal crib_url.")

            stats["reports_saved"] += 1

            links = extract_report_file_links(crib_url)
            stats["files_found"] += len(links)
            if not links:
                _patch_table_rows("annual_reports", {"id": f"eq.{report_id}"}, {"parse_status": "notice_saved_no_files_found"})
                continue

            saved_for_report = 0
            for file_url in links:
                try:
                    content, content_type = download_file(file_url)
                    if not content or len(content) < 50:
                        raise RuntimeError("Atsisiustas failas tuscias arba per mazas.")
                    save_annual_report_file(report_id, r.to_dict(), file_url, content, content_type)
                    saved_for_report += 1
                    stats["files_saved"] += 1
                except Exception as file_exc:
                    stats["errors"] += 1
                    stats["error_rows"].append({
                        "issuer": issuer,
                        "title": title,
                        "url": crib_url,
                        "file_url": file_url,
                        "error": str(file_exc),
                    })

            _patch_table_rows(
                "annual_reports",
                {"id": f"eq.{report_id}"},
                {"parse_status": "files_saved" if saved_for_report else "notice_saved_files_failed"},
            )

        except Exception as exc:
            stats["errors"] += 1
            stats["error_rows"].append({
                "issuer": issuer,
                "title": title,
                "url": crib_url,
                "file_url": "",
                "error": str(exc),
            })

    return stats


def load_saved_annual_reports(start_date, end_date) -> pd.DataFrame:
    try:
        start_iso = f"{start_date}T00:00:00"
        end_iso = f"{end_date}T23:59:59"
        rows = _get_table_rows(
            "annual_reports",
            {
                "select": "id,issuer,report_year,report_type,crib_title,crib_category,published_at,parse_status,crib_url",
                "published_at": [f"gte.{start_iso}", f"lte.{end_iso}"],
                "order": "published_at.desc",
            },
        )
        return pd.DataFrame(rows)
    except Exception as exc:
        st.warning(f"Nepavyko nuskaityti annual_reports: {exc}")
        return pd.DataFrame()


def load_saved_annual_files() -> pd.DataFrame:
    try:
        rows = _get_table_rows(
            "annual_report_files",
            {
                "select": "id,annual_report_id,issuer,report_year,file_name,file_type,file_size,storage_path,parse_status,file_url",
                "order": "created_at.desc",
                "limit": "1000",
            },
        )
        return pd.DataFrame(rows)
    except Exception as exc:
        st.warning(f"Nepavyko nuskaityti annual_report_files: {exc}")
        return pd.DataFrame()


# ============================================================
# Streamlit puslapis
# ============================================================


def show_metines_page():
    st.markdown(
        """
        <div class="hero-card">
            <div class="hero-inner">
                <div class="hero-icon">📚</div>
                <div>
                    <h1 class="hero-title">Metinės ataskaitos</h1>
                    <div class="hero-text">
                        CRIB kategorijos „Metinė informacija“ ataskaitų atsisiuntimas ir saugojimas Supabase.
                        Failai saugomi annual_reports / annual_report_files lentelėse ir Supabase Storage bucket annual-reports.
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    with st.sidebar:
        st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
        st.markdown('<div class="sidebar-card-title">📚 Metinės ataskaitos</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-card-subtitle">Pasirink laikotarpį pagal CRIB paskelbimo datą ir atsisiųsk metines ataskaitas.</div>',
            unsafe_allow_html=True,
        )
        start_date = st.date_input("Nuo", value=date(date.today().year - 2, 1, 1), key="metines_start_date")
        end_date = st.date_input("Iki", value=date.today(), key="metines_end_date")
        run_btn = st.button("Atsisiųsti metines ataskaitas", type="primary", use_container_width=True, key="metines_download_btn")
        st.markdown("</div>", unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    c1.metric("Nuo", str(start_date or "-"))
    c2.metric("Iki", str(end_date or "-"))
    st.markdown("---")

    if start_date > end_date:
        st.error("Data „Nuo“ negali būti vėlesnė už datą „Iki“.")
        st.stop()

    if run_btn:
        progress_box = st.empty()

        def progress(msg: str):
            progress_box.info(msg)

        try:
            with st.spinner("Ieškomos ir saugomos metinės ataskaitos..."):
                stats = update_annual_reports_for_period(start_date, end_date, progress=progress)

            st.success(
                "Metinių ataskaitų atsisiuntimas baigtas: "
                f"VLN emitentų {stats.get('issuers_loaded', 0)}, "
                f"rasta CRIB metinių pranešimų {stats.get('annual_notices_found', 0)}, "
                f"išsaugota annual_reports {stats.get('reports_saved', 0)}, "
                f"rasta failų {stats.get('files_found', 0)}, "
                f"išsaugota failų {stats.get('files_saved', 0)}, "
                f"klaidų {stats.get('errors', 0)}."
            )

            if stats.get("error_rows"):
                st.warning("Dalis įrašų neišsisaugojo. Žemiau pateikiamos klaidos.")
                st.dataframe(pd.DataFrame(stats["error_rows"]), use_container_width=True, hide_index=True)

            progress_box.empty()
        except Exception as exc:
            st.error("Nepavyko atsisiųsti / išsaugoti metinių ataskaitų.")
            st.exception(exc)
            st.code(traceback.format_exc())
            st.stop()

    reports_df = load_saved_annual_reports(start_date, end_date)
    files_df = load_saved_annual_files()

    st.subheader("Išsaugotos metinės ataskaitos")
    if reports_df.empty:
        st.info("Pasirinktu laikotarpiu annual_reports lentelėje įrašų nėra.")
    else:
        show_cols = [c for c in ["issuer", "report_year", "published_at", "parse_status", "crib_title", "crib_url"] if c in reports_df.columns]
        st.dataframe(reports_df[show_cols], use_container_width=True, hide_index=True)

    st.subheader("Išsaugoti metinių ataskaitų failai")
    if files_df.empty:
        st.info("annual_report_files lentelėje failų nėra.")
    else:
        show_cols = [c for c in ["issuer", "report_year", "file_type", "file_size", "parse_status", "file_name", "storage_path", "file_url"] if c in files_df.columns]
        st.dataframe(files_df[show_cols], use_container_width=True, hide_index=True)


# Suderinamumo aliasas, jei app.py importuotų kitu pavadinimu.
show_annual_reports_page = show_metines_page


if __name__ == "__main__":
    show_metines_page()

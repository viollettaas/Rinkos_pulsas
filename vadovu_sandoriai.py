# -*- coding: utf-8 -*-

import os
import re
import warnings
from datetime import date, timedelta
from io import BytesIO
from urllib.parse import urljoin

import pandas as pd
import streamlit as st
import requests
import pdfplumber
import urllib3
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=UserWarning)
os.environ.setdefault("WDM_SSL_VERIFY", "0")

from supabase_cache import load_news_df

try:
    from supabase_cache import load_manager_transactions_df
except Exception:
    load_manager_transactions_df = None


MANAGER_CATEGORY_TOKENS = (
    "pranešimai apie vadovų sandorius",
    "pranesimai apie vadovu sandorius",
    "notifications on transactions concluded by managers",
    "vadovų sandori",
    "vadovu sandori",
)

MANAGER_TRANSACTION_COLUMNS = {
    "crib_url", "crib_title", "crib_category", "published_at", "pdf_url", "pdf_name",
    "issuer", "lei", "person_name", "person_role", "isin", "instrument",
    "transaction_type", "price", "quantity", "transaction_date", "venue", "raw_text",
    "parse_status", "price_quantity_note",
}

# Įrašai su šiais statusais / pastabomis yra techniniai nesėkmingo PDF
# nuskaitymo rezultatai. Juos laikome ne ataskaitos duomenimis, todėl
# Streamlit lentelėje jų nerodome ir į santraukas neįtraukiame.
HIDDEN_MANAGER_PARSE_STATUSES = {
    "pdf_parse_empty_after_retry",
    "pdf_text_empty",
    "pdf_parse_error",
    "pdf_repair_error",
}

HIDDEN_MANAGER_NOTE_TOKENS = (
    "pakartotinai nepavyko nuskaityti pdf teksto",
    "db raw_text buvo tuščias",
    "db raw_text buvo tuscias",
)


# ------------------------------------------------------------
# Bendros pagalbinės funkcijos
# ------------------------------------------------------------

def _notify(progress, message: str):
    if progress:
        progress(message)


def _init_driver(headless: bool = True):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1600,1200")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=lt")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors=yes")
    options.set_capability("acceptInsecureCerts", True)
    return webdriver.Chrome(options=options)


def _supabase_client_parts():
    from supabase_cache import _supabase_headers, _supabase_rest_url, _http_client
    return _supabase_headers, _supabase_rest_url, _http_client


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _to_iso_timestamp(value):
    try:
        if value is not None and not pd.isna(value):
            return pd.to_datetime(value).isoformat()
    except Exception:
        pass
    return None


def _filter_row_for_manager_transactions(row: dict) -> dict:
    clean = {k: v for k, v in (row or {}).items() if k in MANAGER_TRANSACTION_COLUMNS}
    if "issuer" in clean and not _is_empty_db_value(clean.get("issuer")):
        clean["issuer"] = _canonical_issuer_name(clean.get("issuer"))
    return clean


def _is_good_parsed_row(row: dict) -> bool:
    return bool(
        str(row.get("issuer") or "").strip()
        and str(row.get("person_name") or "").strip()
        and str(row.get("transaction_date") or "").strip()
        and str(row.get("isin") or "").strip()
    )


def _is_hidden_manager_report_row(row: dict) -> bool:
    """Ar eilutė yra techninis nepavykusio PDF nuskaitymo įrašas, kurio ataskaitoje nerodome."""
    status = str((row or {}).get("parse_status") or "").strip().lower()
    note = str((row or {}).get("price_quantity_note") or "").strip().lower()
    issuer = str((row or {}).get("issuer") or "").strip()
    raw_text = str((row or {}).get("raw_text") or "").strip()

    if status in HIDDEN_MANAGER_PARSE_STATUSES:
        return True
    if any(token in note for token in HIDDEN_MANAGER_NOTE_TOKENS):
        return True
    # Tuščias emitentas beveik visada reiškia, kad PDF nebuvo sėkmingai išparsintas.
    # Tokios eilutės gadina santraukas ir atrodo kaip pasikartojimai.
    if not issuer and (not raw_text or status in {"parsed_incomplete", "repaired_partial_fields", ""}):
        return True
    return False


def _filter_hidden_manager_report_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Pašalina techninius / tuščius PDF įrašus prieš rodant Streamlit ataskaitoje."""
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    for col in ["issuer", "parse_status", "price_quantity_note", "raw_text"]:
        if col not in df.columns:
            df[col] = ""

    mask_hidden = df.apply(lambda r: _is_hidden_manager_report_row(r.to_dict()), axis=1)
    df = df[~mask_hidden].copy()

    # Papildoma apsauga nuo to paties sandorio dubliavimo, jei jis į DB pateko keliais keliais.
    dedup_cols = [
        "pdf_url", "crib_url", "issuer", "person_name", "transaction_date",
        "isin", "transaction_type", "price", "quantity",
    ]
    existing = [c for c in dedup_cols if c in df.columns]
    if existing:
        sort_cols = [c for c in ["created_at", "id"] if c in df.columns]
        if sort_cols:
            df = df.sort_values(sort_cols, ascending=True)
        df = df.drop_duplicates(subset=existing, keep="last")

    return df.reset_index(drop=True)


# ------------------------------------------------------------
# Emitentu pavadinimu suvienodinimas pagal market_issuers
# ------------------------------------------------------------

_ISSUER_LOOKUP_CACHE = None


def _issuer_norm_key(value) -> str:
    """Suvienodintas raktas emitentu palyginimui."""
    s = str(value or "").lower().strip()
    repl = str.maketrans({"ą":"a","č":"c","ę":"e","ė":"e","į":"i","š":"s","ų":"u","ū":"u","ž":"z"})
    s = s.translate(repl)
    s = re.sub(r"\b(ab|uab|as|asa|akcine bendrove|uzdaroji akcine bendrove)\b", " ", s)
    s = s.replace(" group", " ").replace(" grupe", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load_issuer_lookup_from_market_issuers() -> dict:
    global _ISSUER_LOOKUP_CACHE
    if _ISSUER_LOOKUP_CACHE is not None:
        return _ISSUER_LOOKUP_CACHE

    lookup = {}
    try:
        _headers, _url, _client = _supabase_client_parts()
        url = _url("market_issuers")
        params = {
            "select": "issuer,company,issuer_norm,company_norm,ticker",
            "market": "eq.VLN",
            "order": "issuer.asc",
        }
        with _client() as client:
            resp = client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            rows = resp.json() or []
        for r in rows:
            canonical = str(r.get("issuer") or r.get("company") or "").strip()
            if not canonical:
                continue
            for c in [canonical, r.get("issuer"), r.get("company"), r.get("issuer_norm"), r.get("company_norm"), r.get("ticker")]:
                key = _issuer_norm_key(c)
                if key:
                    lookup[key] = canonical
    except Exception:
        lookup = {}

    _ISSUER_LOOKUP_CACHE = lookup
    return lookup


def _canonical_issuer_name(value: str) -> str:
    original = str(value or "").strip()
    if not original:
        return ""
    lookup = _load_issuer_lookup_from_market_issuers()
    key = _issuer_norm_key(original)
    if key in lookup:
        return lookup[key]
    for k, canonical in lookup.items():
        if key and k and (key in k or k in key):
            return canonical
    return original


# ------------------------------------------------------------
# Supabase manager_transactions CRUD
# ------------------------------------------------------------

def _manager_pdf_already_saved(pdf_url: str) -> bool:
    if not pdf_url:
        return False
    try:
        _headers, _url, _client = _supabase_client_parts()
        url = _url("manager_transactions")
        params = {"select": "id,pdf_url", "pdf_url": f"eq.{pdf_url}", "limit": "1"}
        with _client() as client:
            resp = client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            return bool(resp.json() or [])
    except Exception:
        return False


def _manager_transaction_already_saved_by_signature(row: dict) -> bool:
    try:
        _headers, _url, _client = _supabase_client_parts()
        url = _url("manager_transactions")
        params = {
            "select": "id,pdf_url",
            "crib_url": f"eq.{row.get('crib_url', '')}",
            "issuer": f"eq.{row.get('issuer', '')}",
            "person_name": f"eq.{row.get('person_name', '')}",
            "transaction_date": f"eq.{row.get('transaction_date', '')}",
            "isin": f"eq.{row.get('isin', '')}",
            "limit": "1",
        }
        with _client() as client:
            resp = client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            return bool(resp.json() or [])
    except Exception:
        return False


def _post_manager_transaction(row: dict) -> bool:
    _headers, _url, _client = _supabase_client_parts()
    url = _url("manager_transactions")
    clean_row = _filter_row_for_manager_transactions(row)

    with _client() as client:
        resp = client.post(
            url,
            headers={**_headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json=clean_row,
        )
        if resp.status_code in (200, 201, 204):
            return True
        if resp.status_code == 409:
            return False
        raise RuntimeError(f"Supabase manager_transactions įrašymo klaida: {resp.status_code} - {resp.text}")


def _update_manager_transaction_by_id(row_id: int, parsed_row: dict) -> bool:
    _headers, _url, _client = _supabase_client_parts()
    url = _url("manager_transactions")
    clean_row = _filter_row_for_manager_transactions(parsed_row)
    clean_row.pop("id", None)

    with _client() as client:
        resp = client.patch(
            url,
            headers={**_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{row_id}"},
            json=clean_row,
        )
        if resp.status_code in (200, 204):
            return True
        raise RuntimeError(f"Supabase manager_transactions update klaida: {resp.status_code} - {resp.text}")


def _is_empty_db_value(value) -> bool:
    """Ar manager_transactions laukas laikytinas tuščiu."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    s = str(value).strip()
    if s == "":
        return True
    if s.lower() in {"nan", "none", "null", "nat"}:
        return True
    return False


def _looks_like_valid_isin(value) -> bool:
    """Tikras ISIN turi prasidėti valstybės kodu ir turėti 12 simbolių.
    Šis patikrinimas neleidžia ISIN lauke palikti tokių žodžių kaip VADOVAUJAMAS.
    """
    if _is_empty_db_value(value):
        return False
    s = str(value).strip().upper()
    return bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", s)) and s[:2] in {"LT", "LV", "EE"}


def _is_bad_existing_value(col: str, value) -> bool:
    """Ar DB reikšmė nėra tuščia, bet aiškiai blogai nuskaityta ir ją reikia perrašyti."""
    if _is_empty_db_value(value):
        return True
    s = str(value).strip()
    s_l = s.lower()

    if col == "isin":
        return not _looks_like_valid_isin(s)

    if col in {"issuer", "person_name"}:
        bad_tokens = ["/ vardas", "pavardė", "pavarde", "vadovaujamas", "pareigas einančio"]
        return any(t in s_l for t in bad_tokens)

    if col == "instrument":
        return ("kaina" in s_l and "kiekis" in s_l) or ("identifikavimo" in s_l and len(s) > 80)

    if col == "transaction_type":
        return len(s) > 220 or "kaina(-os)" in s_l or "apimtis" in s_l

    if col == "venue":
        return len(s) > 260 or "vadovaujamas pareigas" in s_l or "pasiraš" in s_l or "pasiras" in s_l

    return False


def _has_useful_value(value) -> bool:
    return not _is_empty_db_value(value)


def _update_manager_transaction_empty_fields_by_id(row_id: int, current_row: dict, parsed_row: dict) -> bool:
    """
    Atnaujina tuščius laukus pagal id. Taip pat perrašo kelias aiškiai blogas
    senas reikšmes, pvz. ISIN='VADOVAUJAMAS' arba asmenį su '/ vardas'.
    Gerų reikšmių tuščiomis neperrašome.
    """
    update = {}

    fill_if_empty_or_bad = [
        "issuer", "lei", "person_name", "person_role", "isin", "instrument",
        "transaction_type", "price", "quantity", "transaction_date", "venue",
        "crib_title", "crib_category", "pdf_name", "price_quantity_note",
    ]

    for col in fill_if_empty_or_bad:
        new_val = parsed_row.get(col)
        old_val = current_row.get(col)
        if col == "issuer" and _has_useful_value(new_val):
            new_val = _canonical_issuer_name(new_val)
        if (_is_empty_db_value(old_val) or _is_bad_existing_value(col, old_val)) and _has_useful_value(new_val):
            # Neįrašome akivaizdžiai blogos naujos reikšmės.
            if not _is_bad_existing_value(col, new_val):
                update[col] = new_val

    old_issuer = current_row.get("issuer")
    canonical_old_issuer = _canonical_issuer_name(old_issuer)
    if _has_useful_value(old_issuer) and canonical_old_issuer and canonical_old_issuer != str(old_issuer).strip():
        update["issuer"] = canonical_old_issuer

    # raw_text atnaujiname, jei DB tuščias arba naujas tekstas ilgesnis.
    new_raw = str(parsed_row.get("raw_text") or "")
    old_raw = str(current_row.get("raw_text") or "")
    if new_raw and (not old_raw.strip() or len(new_raw) > len(old_raw)):
        update["raw_text"] = new_raw

    # pdf_url keičiame tik jei senas tuščias.
    if _is_empty_db_value(current_row.get("pdf_url")) and _has_useful_value(parsed_row.get("pdf_url")):
        update["pdf_url"] = parsed_row.get("pdf_url")

    if _is_empty_db_value(current_row.get("published_at")) and _has_useful_value(parsed_row.get("published_at")):
        update["published_at"] = parsed_row.get("published_at")

    merged = dict(current_row)
    merged.update(update)
    if _is_good_parsed_row(merged):
        update["parse_status"] = "repaired_empty_fields"
    elif update:
        update["parse_status"] = "repaired_partial_fields"
    else:
        update["parse_status"] = "pdf_parse_empty_after_retry"

    if not update:
        return False

    return _update_manager_transaction_by_id(row_id, update)


def _delete_manager_transaction_by_id(row_id: int) -> bool:
    _headers, _url, _client = _supabase_client_parts()
    url = _url("manager_transactions")
    with _client() as client:
        resp = client.delete(
            url,
            headers={**_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{row_id}"},
        )
        if resp.status_code in (200, 204):
            return True
        raise RuntimeError(f"Supabase manager_transactions delete klaida: {resp.status_code} - {resp.text}")


def _delete_manager_transactions_for_crib_url(crib_url: str) -> int:
    if not crib_url:
        return 0
    _headers, _url, _client = _supabase_client_parts()
    url = _url("manager_transactions")
    with _client() as client:
        resp = client.delete(
            url,
            headers={**_headers(), "Prefer": "return=representation"},
            params={"crib_url": f"eq.{crib_url}"},
        )
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"Supabase manager_transactions delete klaida: {resp.status_code} - {resp.text}")
        try:
            return len(resp.json() or [])
        except Exception:
            return 0


def delete_hidden_manager_report_rows(limit: int = 1000) -> dict:
    """Iš DB pašalina techninius nepavykusio PDF nuskaitymo įrašus.

    Naudoti nebūtina, nes ataskaita juos jau filtruoja, bet mygtukas praverčia
    norint susitvarkyti senus įrašus, kuriuose yra pastaba
    „Pakartotinai nepavyko nuskaityti PDF teksto...“.
    """
    stats = {"found": 0, "deleted": 0, "errors": 0}
    try:
        _headers, _url, _client = _supabase_client_parts()
        url = _url("manager_transactions")
        params = {
            "select": "id,issuer,raw_text,parse_status,price_quantity_note",
            "order": "id.desc",
            "limit": str(limit),
        }
        with _client() as client:
            resp = client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            rows = resp.json() or []

        hidden_ids = [int(r["id"]) for r in rows if r.get("id") and _is_hidden_manager_report_row(r)]
        stats["found"] = len(hidden_ids)

        for row_id in hidden_ids:
            try:
                if _delete_manager_transaction_by_id(row_id):
                    stats["deleted"] += 1
            except Exception:
                stats["errors"] += 1

    except Exception:
        stats["errors"] += 1

    return stats


def _has_good_transaction_for_crib_url(crib_url: str) -> bool:
    if not crib_url:
        return False
    try:
        _headers, _url, _client = _supabase_client_parts()
        url = _url("manager_transactions")
        params = {
            "select": "id,issuer,person_name,transaction_date,isin,parse_status",
            "crib_url": f"eq.{crib_url}",
            "limit": "20",
        }
        with _client() as client:
            resp = client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data = resp.json() or []
        for r in data:
            if _is_good_parsed_row(r):
                return True
        return False
    except Exception:
        return False


# ------------------------------------------------------------
# CRIB / PDF nuskaitymas
# ------------------------------------------------------------

def _rank_pdf_links(links: list[str]) -> list[str]:
    """CRIB puslapyje dažnai būna dvi nuorodos į tą patį PDF:
    1) https://www.crib.lt/cns-web/oam/viewAttachment.action?...  -- patikima;
    2) https://ml-eu.globenewswire.com/Resource/Download/...       -- dažnai grąžina tuščią / ne PDF turinį.
    Todėl pirmiausia imame CRIB attachment nuorodas, o globenewswire paliekame tik atsargai.
    """
    seen = set()
    clean = []
    for link in links or []:
        if not link:
            continue
        link = link.strip()
        if link in seen:
            continue
        seen.add(link)
        clean.append(link)

    def score(u: str) -> int:
        u_l = u.lower()
        if "crib.lt/cns-web/oam/viewattachment" in u_l:
            return 0
        if "viewattachment.action" in u_l:
            return 1
        if "globenewswire.com/resource/download" in u_l:
            return 9
        return 5

    return sorted(clean, key=score)


def _extract_pdf_links_from_html(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = (a.get_text(" ", strip=True) or "").lower()
        href_l = href.lower()
        if ".pdf" in href_l or "viewattachment.action" in href_l or "download" in href_l or "attachment" in href_l or "pdf" in text:
            full = urljoin(base_url, href)
            if full not in links:
                links.append(full)
    return _rank_pdf_links(links)


def _extract_pdf_links_from_crib_page(driver, crib_url: str) -> list[str]:
    if not crib_url:
        return []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    try:
        resp = requests.get(crib_url, headers=headers, verify=False, timeout=25)
        if resp.ok and resp.text:
            links = _extract_pdf_links_from_html(resp.text, crib_url)
            if links:
                return links
    except Exception:
        pass

    main_handle = None
    try:
        main_handle = driver.current_window_handle
        driver.execute_script("window.open('about:blank','_blank');")
        driver.switch_to.window(driver.window_handles[-1])
        driver.get(crib_url)
        try:
            WebDriverWait(driver, 18).until(lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"})
        except Exception:
            pass
        links = _extract_pdf_links_from_html(driver.page_source, crib_url)
        driver.close()
        driver.switch_to.window(main_handle)
        return links
    except Exception:
        try:
            if len(driver.window_handles) > 1:
                driver.close()
            if main_handle:
                driver.switch_to.window(main_handle)
        except Exception:
            pass
        return []


def _download_pdf_bytes(pdf_url: str) -> bytes:
    if not pdf_url:
        return b""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/pdf,application/octet-stream,*/*",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(pdf_url, headers=headers, verify=False, timeout=45, allow_redirects=True)
    resp.raise_for_status()
    content = resp.content or b""
    # Kai kurių ml-eu.globenewswire nuorodų turinys nėra PDF arba yra tuščias.
    if len(content) < 100 or not content[:20].lstrip().startswith(b"%PDF"):
        return b""
    return content


def _extract_pdf_text(pdf_url: str) -> str:
    content = _download_pdf_bytes(pdf_url)
    if not content:
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


# ------------------------------------------------------------
# PDF parseris
# ------------------------------------------------------------

def _parse_number(value: str, as_int: bool = False):
    if value is None:
        return None
    s = str(value).strip()
    m = re.search(r"[-+]?\d[\d\s.,]*", s)
    if not m:
        return None
    num = m.group(0).replace(" ", "")
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    else:
        num = num.replace(",", ".")
    try:
        val = float(num)
        return int(round(val)) if as_int else val
    except Exception:
        return None


def _regex_value(text: str, pattern: str) -> str:
    m = re.search(pattern, text or "", flags=re.I | re.S)
    if not m:
        return ""
    return _collapse_ws(m.group(1))


def _text_after_label(text: str, labels, max_len: int = 160) -> str:
    if not text:
        return ""
    for label in labels:
        pattern = rf"(?:^|\n|\s){label}\s*[:\-]?\s*(.+?)(?=\n[a-z]\)|\n\d\.|\n[A-ZĄČĘĖĮŠŲŪŽ][^\n]{{0,80}}\s*[:\-]?|$)"
        m = re.search(pattern, text, flags=re.I | re.S)
        if m:
            value = _collapse_ws(m.group(1))
            return value[:max_len]
    return ""


def _extract_date_from_text(text: str):
    if not text:
        return None
    patterns = [
        (r"(\d{4}[-.]\d{2}[-.]\d{2})", False),
        (r"(\d{2}[-.]\d{2}[-.]\d{4})", True),
    ]
    for pat, dayfirst in patterns:
        m = re.search(pat, text)
        if m:
            dt = pd.to_datetime(m.group(1), errors="coerce", dayfirst=dayfirst)
            if pd.notna(dt):
                return dt.date().isoformat()
    return None


def _clean_person_name(value: str) -> str:
    v = _collapse_ws(value)
    if not v:
        return ""
    v = re.sub(r"^[/\\]?\s*vardas\s*,?\s*", "", v, flags=re.I)
    v = re.sub(r"\bpavard[ėe]\b", "", v, flags=re.I)
    v = re.sub(r"\bvardas\b", "", v, flags=re.I)
    v = _collapse_ws(v.strip(" ,;:-"))
    # Jei liko daug teksto, paimame pirmą dviejų žodžių asmens vardą.
    m = re.search(r"([A-ZĄČĘĖĮŠŲŪŽ][a-ząčęėįšųūž]+\s+[A-ZĄČĘĖĮŠŲŪŽ][a-ząčęėįšųūž]+(?:-[A-ZĄČĘĖĮŠŲŪŽ][a-ząčęėįšųūž]+)?)", v)
    if m:
        return _collapse_ws(m.group(1))
    return v[:140]


def _clean_issuer(value: str) -> str:
    v = _collapse_ws(value)
    if not v:
        return ""
    v = re.sub(r"\bLEI\b.*$", "", v, flags=re.I).strip(" ,;:-")
    return _collapse_ws(v)[:180]


def _extract_person_fallback(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r"1\..*?a\)\s*Pavadinimas\s+(.+?)\s+2\.",
        r"(?:Vadovaujamas pareigas einančio asmens|Vadovo|Asmens)\s+(?:vardas ir pavardė|vardas,?\s*pavardė)\s+(.+?)(?:\n|$)",
        r"(?:Name of the person|Person name)\s+(.+?)(?:\n|$)",
        r"([A-ZĄČĘĖĮŠŲŪŽ][a-ząčęėįšųūž]+\s+[A-ZĄČĘĖĮŠŲŪŽ][a-ząčęėįšųūž]+(?:-[A-ZĄČĘĖĮŠŲŪŽ][a-ząčęėįšųūž]+)?),\s*(?:AB|Akcinė bendrovė|UAB|AS|A/S)",
        r"(Darius\s+Šulnis|Artūras\s+Šilinis|Andrius\s+Pranckevičius|Regina\s+Kvaraciejienė|Rokas\s+Kvaraciejus|Eglė\s+Kvaraciejūtė-Ivanauskienė)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I | re.S)
        if m:
            val = _clean_person_name(m.group(1))
            if val:
                return val
    return ""


def _extract_issuer_fallback(text: str, role: str = "", venue: str = "", crib_title: str = "") -> str:
    combined = "\n".join([text or "", role or "", venue or "", crib_title or ""])
    patterns = [
        r"3\..*?a\)\s*Pavadinimas\s+(.+?)\s+b\)\s*LEI",
        r"(?:Emitento pavadinimas|Issuer name|Name of the issuer)\s+(.+?)(?:\n|LEI|$)",
        r"(Akcinė\s+bendrovė\s+[„\"A-ZĄČĘĖĮŠŲŪŽ][^\n,;]{2,80})",
        r"(AB\s+[„\"A-ZĄČĘĖĮŠŲŪŽ][^\n,;]{2,80})\s+(?:vadovas|valdybos|stebėtojų|stebetojų)",
        r",\s*(AB\s+[„\"A-ZĄČĘĖĮŠŲŪŽ][^,;\n]{2,80})\s+vadov",
        r"(AB\s+Akola\s+group)",
        r"(AB\s+„Invalda\s+INVL“)",
    ]
    for pat in patterns:
        m = re.search(pat, combined, flags=re.I | re.S)
        if m:
            val = _clean_issuer(m.group(1))
            if val:
                return val
    return ""


def _clean_instrument(value: str) -> str:
    v = _collapse_ws(value)
    v = re.sub(r"Finansinės priemonės", "", v, flags=re.I)
    v = re.sub(r"aprašymas,?\s*priemonės\s*rūšis", "", v, flags=re.I)
    v = re.sub(r"Identifikavimo\s+kodas", "", v, flags=re.I)
    v = re.sub(r"ISIN\s*(?:kodas)?\s*[:\-]?\s*(?:LT|LV|EE)[A-Z0-9]{10}", "", v, flags=re.I)
    v = re.sub(r"Kaina\(-?os\)?.*$", "", v, flags=re.I | re.S)
    return _collapse_ws(v)[:220]


def _extract_isin(text: str) -> str:
    """Griežtas ISIN ištraukimas.

    Ankstesnė logika su re.I leisdavo paimti 12 raidžių žodžius, pvz.
    VADOVAUJAMAS. Dabar pirmiausia ieškome tik po aiškaus ISIN labelio,
    o atsarginiu atveju leidžiame tik Baltijos ISIN prefiksus LT/LV/EE.
    """
    if not text:
        return ""

    patterns = [
        r"ISIN\s*(?:kodas|code)?\s*[:\-]?\s*((?:LT|LV|EE)[A-Z0-9]{10})",
        r"Identifikavimo\s+kodas\s*(?:ISIN\s*kodas)?\s*[:\-]?\s*((?:LT|LV|EE)[A-Z0-9]{10})",
        r"\b((?:LT|LV|EE)[A-Z0-9]{10})\b",
    ]

    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            isin = m.group(1).upper().strip()
            if _looks_like_valid_isin(isin):
                return isin
    return ""


def _parse_manager_transaction_pdf_text(text: str, pdf_url: str, crib_url: str, published_at=None, crib_title: str = "", crib_category: str = "") -> dict:
    text = text or ""
    norm_text = _collapse_ws(text)

    person = _regex_value(text, r"1\..*?a\)\s*Pavadinimas\s+(.+?)\s+2\.")
    role = _regex_value(text, r"Pareigos\s*/\s*statusas\s+(.+?)\s+b\)\s*Pirminis")
    issuer = _regex_value(text, r"3\..*?a\)\s*Pavadinimas\s+(.+?)\s+b\)\s*LEI")
    lei = _regex_value(text, r"b\)\s*LEI\s+([A-Z0-9]{18,20})")
    transaction_type = _regex_value(text, r"b\)\s*Sandorio pobūdis\s+(.+?)\s+c\)\s*Kaina")
    venue = _regex_value(text, r"f\)\s*Sandorio vieta\s+(.+?)\s*$")
    transaction_date = _regex_value(text, r"e\)\s*Sandorio data\s+(\d{4}[-.]\d{2}[-.]\d{2})") or _extract_date_from_text(text)

    # Alternatyvios formos, kur laukai vadinasi kitaip.
    if not person:
        person = _text_after_label(text, [
            r"Vadovaujamas pareigas einančio asmens vardas ir pavardė",
            r"Vadovo vardas ir pavardė",
            r"Asmens vardas ir pavardė",
            r"Name of the person",
            r"Person name",
        ], max_len=180)
    person = _clean_person_name(person) or _extract_person_fallback(text)

    if not role:
        role = _text_after_label(text, [
            r"Pareigos / statusas",
            r"Vadovaujamas pareigas einančio asmens pareigos",
            r"Pareigos",
            r"Statusas",
            r"Position",
            r"Function",
            r"Role",
        ], max_len=220)
    role = _collapse_ws(role)

    if not issuer:
        issuer = _text_after_label(text, [
            r"Emitento pavadinimas",
            r"Issuer name",
            r"Name of the issuer",
        ], max_len=180)
    issuer = _clean_issuer(issuer) or _extract_issuer_fallback(text, role=role, venue=venue, crib_title=crib_title)

    if not transaction_type:
        transaction_type = _text_after_label(text, [
            r"Sandorio pobūdis",
            r"Sandorio rūšis",
            r"Nature of the transaction",
            r"Transaction type",
        ], max_len=220)
    transaction_type = _collapse_ws(re.sub(r"Kaina\(-?os\)?.*$", "", transaction_type or "", flags=re.I | re.S))

    if not venue:
        venue = _text_after_label(text, [
            r"Sandorio vieta",
            r"Prekybos vieta",
            r"Place of the transaction",
            r"Trading venue",
            r"Venue",
        ], max_len=220)
    venue = _collapse_ws(re.sub(r"(?:Pagal|Under the power|Vadovaujamas pareigas|pasiraš).*", "", venue or "", flags=re.I | re.S))

    isin = _extract_isin(text)
    if isin and not _looks_like_valid_isin(isin):
        isin = ""

    instrument_block = _regex_value(text, r"a\)\s*Finansinės priemonės\s+(.+?)\s+b\)\s*Sandorio pobūdis")
    if not instrument_block:
        instrument_block = _text_after_label(text, [
            r"Finansinės priemonės aprašymas.*?Identifikavimo kodas",
            r"Finansinė priemonė",
            r"Financial instrument",
        ], max_len=260)
    instrument = _clean_instrument(instrument_block)

    price = None
    quantity = None

    # LT MAR forma: Kaina Kiekis / 1,66 EUR 78 718
    pq = re.search(r"Kaina\s+Kiekis\s+([\d\s]+(?:[,.]\d+)?)\s*(?:EUR|€)?\s+([\d\s]+)", text, flags=re.I)
    if not pq:
        # Kita forma: Kaina(-os) Apimtis / 0,00 EUR 2 340 249
        pq = re.search(r"Kaina\(-?os\)?\s+Apimtis\s+([\d\s]+(?:[,.]\d+)?)\s*(?:EUR|€)?\s+([\d\s]+)", text, flags=re.I)
    if pq:
        price = _parse_number(pq.group(1))
        quantity = _parse_number(pq.group(2), as_int=True)

    if quantity is None:
        q = re.search(r"(?:Akcijų\s+kiekis|apibendrinta\s+apimtis)\s*[:\-]?\s*([\d\s]+)", text, flags=re.I)
        if q:
            quantity = _parse_number(q.group(1), as_int=True)
    if price is None:
        pr = re.search(r"(?:Vienos\s+akcijos\s+kaina|kaina)\s*[:\-]?\s*([\d\s]+(?:[,.]\d+)?)\s*(?:EUR|€)", text, flags=re.I)
        if pr:
            price = _parse_number(pr.group(1))

    pdf_name = ""
    try:
        pdf_name = pdf_url.split("/")[-1].split("?")[0]
    except Exception:
        pass

    issuer = _canonical_issuer_name(issuer)

    status = "parsed_mar_form" if norm_text else "pdf_text_empty"
    row = {
        "published_at": _to_iso_timestamp(published_at),
        "transaction_date": transaction_date,
        "issuer": issuer,
        "lei": lei,
        "person_name": person,
        "person_role": role,
        "isin": isin,
        "instrument": instrument,
        "transaction_type": transaction_type,
        "price": price,
        "quantity": quantity,
        "venue": venue,
        "parse_status": status,
        "price_quantity_note": "",
        "pdf_url": pdf_url,
        "pdf_name": pdf_name,
        "crib_url": crib_url,
        "crib_title": crib_title or "",
        "crib_category": crib_category or "",
        "raw_text": text[:12000] if text else "",
    }

    # Jei nėra kainos/kiekio dėl paveldėjimo ar įkeitimo, bet pagrindiniai laukai yra, nelaikome tuščiu parseriu.
    if status == "parsed_mar_form" and not _is_good_parsed_row(row):
        row["parse_status"] = "parsed_incomplete"
    return row


# ------------------------------------------------------------
# Naujų vadovų sandorių įrašymas iš market_news
# ------------------------------------------------------------

def save_manager_transactions_from_crib_selenium(driver, crib_url: str, published_at=None, crib_title: str = "", crib_category: str = "") -> int:
    if not crib_url:
        return 0

    pdf_links = _extract_pdf_links_from_crib_page(driver, crib_url)
    if not pdf_links:
        return 0

    saved = 0
    for pdf_url in pdf_links:
        if _manager_pdf_already_saved(pdf_url):
            continue
        try:
            text = _extract_pdf_text(pdf_url)
            if not text:
                # Tuščių / ne PDF mirror nuorodų nebeįrašome į DB.
                continue
            row = _parse_manager_transaction_pdf_text(
                text,
                pdf_url=pdf_url,
                crib_url=crib_url,
                published_at=published_at,
                crib_title=crib_title,
                crib_category=crib_category,
            )
            if not _is_good_parsed_row(row):
                # Nekuriame naujų tuščių eilučių. Blogus senuosius įrašus tvarko repair funkcija.
                continue
            if _manager_transaction_already_saved_by_signature(row):
                continue
            if _post_manager_transaction(row):
                saved += 1
        except Exception:
            # Sąmoningai nebeįrašome pdf_parse_error eilučių, nes jos vėliau teršia lentelę.
            continue
    return saved


def _is_manager_notice(row) -> bool:
    text = " ".join([
        str(row.get("category", "") or ""),
        str(row.get("title", "") or ""),
        str(row.get("content", "") or "")[:500],
    ]).lower()
    return any(token in text for token in MANAGER_CATEGORY_TOKENS)


def _load_recent_manager_crib_notices(days_back: int = 45) -> pd.DataFrame:
    start = date.today() - timedelta(days=days_back)
    end = date.today()
    df = load_news_df("crib", start, end)

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    for col in ["category", "title", "url", "published_at", "content", "company"]:
        if col not in df.columns:
            df[col] = ""

    mask = df.apply(_is_manager_notice, axis=1)
    df = df[mask].copy()

    if df.empty:
        return df

    df["published_at_dt"] = pd.to_datetime(df["published_at"], errors="coerce", utc=False)
    df = df.sort_values("published_at_dt", ascending=False)
    df = df.drop_duplicates(subset=["url"], keep="first")
    return df.reset_index(drop=True)


def update_manager_transactions_from_recent_crib(days_back: int = 45, max_messages: int = 30, headless: bool = True, progress=None) -> dict:
    stats = {
        "manager_messages_found": 0,
        "manager_messages_processed": 0,
        "manager_transactions_saved": 0,
        "manager_transactions_errors": 0,
        "module_available": True,
    }

    notices = _load_recent_manager_crib_notices(days_back=days_back)
    if notices is None or notices.empty:
        return stats

    notices = notices.head(max_messages).copy()
    stats["manager_messages_found"] = len(notices)

    driver = _init_driver(headless=headless)
    try:
        for _, row in notices.iterrows():
            url = str(row.get("url", "") or "").strip()
            if not url:
                continue

            published_at = row.get("published_at", None)
            try:
                published_at = pd.to_datetime(published_at, errors="coerce")
                if pd.isna(published_at):
                    published_at = None
                else:
                    published_at = published_at.to_pydatetime()
            except Exception:
                published_at = None

            stats["manager_messages_processed"] += 1
            _notify(progress, f"Tikrinamas vadovų sandorių CRIB pranešimas: {url}")

            try:
                saved = save_manager_transactions_from_crib_selenium(
                    driver=driver,
                    crib_url=url,
                    published_at=published_at,
                    crib_title=str(row.get("title", "") or ""),
                    crib_category=str(row.get("category", "") or ""),
                )
                stats["manager_transactions_saved"] += int(saved or 0)
            except Exception as exc:
                stats["manager_transactions_errors"] += 1
                _notify(progress, f"Vadovų sandorių PDF klaida: {exc}")

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return stats


# ------------------------------------------------------------
# Blogai nuskaitytų PDF taisymas
# ------------------------------------------------------------

def _load_bad_manager_transactions(limit: int = 200) -> pd.DataFrame:
    _headers, _url, _client = _supabase_client_parts()
    url = _url("manager_transactions")
    params = {
        "select": "id,published_at,pdf_url,pdf_name,crib_url,crib_title,crib_category,issuer,lei,person_name,person_role,isin,instrument,transaction_type,price,quantity,transaction_date,venue,raw_text,parse_status,price_quantity_note",
        "order": "id.desc",
        "limit": str(limit),
    }
    with _client() as client:
        resp = client.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
        data = resp.json() or []

    df = pd.DataFrame(data)
    if df.empty:
        return df

    for col in ["issuer", "person_name", "transaction_date", "isin", "parse_status", "pdf_url"]:
        if col not in df.columns:
            df[col] = ""

    isin_series = df["isin"].fillna("").astype(str).str.strip()
    bad_isin = isin_series.eq("") | (~isin_series.apply(_looks_like_valid_isin))

    issuer_series = df["issuer"].fillna("").astype(str).str.strip()
    issuer_needs_canonical = issuer_series.apply(
        lambda x: bool(x) and _canonical_issuer_name(x) != x
    )

    mask = (
        df["pdf_url"].fillna("").astype(str).str.strip().ne("")
        & (
            df["issuer"].fillna("").astype(str).str.strip().eq("")
            | issuer_needs_canonical
            | df["person_name"].fillna("").astype(str).str.strip().eq("")
            | df["transaction_date"].fillna("").astype(str).str.strip().eq("")
            | bad_isin
            | df["parse_status"].fillna("").astype(str).isin(["pdf_parse_error", "pdf_text_empty", "parsed_incomplete", "pdf_parse_empty_after_retry", "repaired_partial_fields"])
        )
    )
    return df[mask].copy().reset_index(drop=True)


def _try_parse_best_pdf_for_crib(crib_url: str, published_at=None, crib_title: str = "", crib_category: str = "", preferred_pdf_url: str = "") -> dict | None:
    """Bando parsinti nurodytą PDF, o jei jis tuščias - ieško geresnės CRIB attachment nuorodos tame pačiame pranešime."""
    candidate_links = []
    if preferred_pdf_url:
        candidate_links.append(preferred_pdf_url)

    try:
        links = _extract_pdf_links_from_html(requests.get(
            crib_url,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "lt-LT,lt;q=0.9"},
            verify=False,
            timeout=25,
        ).text, crib_url)
        for link in links:
            if link not in candidate_links:
                candidate_links.append(link)
    except Exception:
        pass

    for pdf_url in _rank_pdf_links(candidate_links):
        try:
            text = _extract_pdf_text(pdf_url)
            if not text:
                continue
            parsed = _parse_manager_transaction_pdf_text(
                text,
                pdf_url=pdf_url,
                crib_url=crib_url,
                published_at=published_at,
                crib_title=crib_title,
                crib_category=crib_category,
            )
            if _is_good_parsed_row(parsed):
                return parsed
        except Exception:
            continue
    return None


def repair_bad_manager_transactions(limit: int = 200, progress=None) -> dict:
    """
    Pakartotinai perparsina blogai / tuščiai nuskaitytus manager_transactions įrašus
    ir pagal id užpildo tuščius / aiškiai blogus laukus.

    Papildomai visada suvienodina issuer pagal market_issuers lentelę, net jeigu
    pats issuer laukas nėra tuščias.

    Nieko netrina ir nekuria naujų dublikatų. Jei konkretaus PDF teksto nepavyksta
    paimti, bandoma naudoti:
    1) esamą raw_text iš DB;
    2) kitą PDF / attachment nuorodą iš to paties crib_url.
    """
    stats = {
        "bad_found": 0,
        "repaired": 0,
        "partial": 0,
        "unchanged": 0,
        "failed": 0,
        "deleted_duplicates": 0,
    }

    bad_df = _load_bad_manager_transactions(limit=limit)
    if bad_df is None or bad_df.empty:
        return stats

    stats["bad_found"] = len(bad_df)

    for _, r in bad_df.iterrows():
        current_row = r.to_dict()
        row_id = int(current_row.get("id"))
        pdf_url = str(current_row.get("pdf_url") or "").strip()
        crib_url = str(current_row.get("crib_url") or "").strip()

        if not pdf_url and not crib_url:
            stats["failed"] += 1
            continue

        _notify(progress, f"Taisomas manager_transactions id={row_id}")

        try:
            parsed = None

            # 1) Pirmiausia bandome per esamą PDF URL ir, jei reikia, alternatyvias CRIB PDF nuorodas.
            if crib_url:
                parsed = _try_parse_best_pdf_for_crib(
                    crib_url=crib_url,
                    published_at=current_row.get("published_at"),
                    crib_title=str(current_row.get("crib_title") or ""),
                    crib_category=str(current_row.get("crib_category") or ""),
                    preferred_pdf_url=pdf_url,
                )

            # 2) Jei CRIB alternatyvų nėra, bet turime PDF URL, bandome tiesiogiai.
            if parsed is None and pdf_url:
                try:
                    text = _extract_pdf_text(pdf_url)
                    if text:
                        parsed = _parse_manager_transaction_pdf_text(
                            text,
                            pdf_url=pdf_url,
                            crib_url=crib_url,
                            published_at=current_row.get("published_at"),
                            crib_title=str(current_row.get("crib_title") or ""),
                            crib_category=str(current_row.get("crib_category") or ""),
                        )
                except Exception:
                    parsed = None

            # 3) Jei DB jau turi raw_text, bet seni struktūriniai laukai tušti, perparsiname raw_text.
            if parsed is None and str(current_row.get("raw_text") or "").strip():
                parsed = _parse_manager_transaction_pdf_text(
                    str(current_row.get("raw_text") or ""),
                    pdf_url=pdf_url,
                    crib_url=crib_url,
                    published_at=current_row.get("published_at"),
                    crib_title=str(current_row.get("crib_title") or ""),
                    crib_category=str(current_row.get("crib_category") or ""),
                )

            if parsed is None:
                _update_manager_transaction_by_id(row_id, {
                    "parse_status": "pdf_parse_empty_after_retry",
                    "price_quantity_note": "Pakartotinai nepavyko nuskaityti PDF teksto ir DB raw_text buvo tuščias.",
                })
                stats["failed"] += 1
                continue

            before_good = _is_good_parsed_row(current_row)
            changed = _update_manager_transaction_empty_fields_by_id(row_id, current_row, parsed)

            if not changed:
                stats["unchanged"] += 1
            else:
                merged = dict(current_row)
                # simuliuojame merge pagal tuščių laukų taisyklę
                for k, v in parsed.items():
                    if k in MANAGER_TRANSACTION_COLUMNS and _is_empty_db_value(merged.get(k)) and _has_useful_value(v):
                        merged[k] = v
                if _is_good_parsed_row(merged):
                    stats["repaired"] += 1
                else:
                    stats["partial"] += 1

        except Exception as exc:
            stats["failed"] += 1
            try:
                _update_manager_transaction_by_id(row_id, {
                    "parse_status": "pdf_repair_error",
                    "price_quantity_note": str(exc)[:500],
                })
            except Exception:
                pass

    return stats


def recalc_latest_manager_notice(headless: bool = True, progress=None) -> dict:
    stats = {"notice_found": False, "deleted": 0, "inserted": 0, "errors": 0}

    notices = _load_recent_manager_crib_notices(days_back=45)
    if notices is None or notices.empty:
        return stats

    latest = notices.iloc[0]
    crib_url = str(latest.get("url", "") or "").strip()
    if not crib_url:
        return stats

    stats["notice_found"] = True
    stats["deleted"] = _delete_manager_transactions_for_crib_url(crib_url)

    published_at = latest.get("published_at", None)
    try:
        published_at = pd.to_datetime(published_at, errors="coerce")
        if pd.isna(published_at):
            published_at = None
        else:
            published_at = published_at.to_pydatetime()
    except Exception:
        published_at = None

    driver = _init_driver(headless=headless)
    try:
        stats["inserted"] = save_manager_transactions_from_crib_selenium(
            driver=driver,
            crib_url=crib_url,
            published_at=published_at,
            crib_title=str(latest.get("title", "") or ""),
            crib_category=str(latest.get("category", "") or ""),
        )
    except Exception:
        stats["errors"] += 1
        raise
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return stats


# ------------------------------------------------------------
# Lentelės paruošimas
# ------------------------------------------------------------

def _load_manager_transactions_df_fallback(start_date, end_date) -> pd.DataFrame:
    try:
        from supabase_cache import _supabase_headers, _supabase_rest_url, _http_client
        start_iso = f"{start_date}T00:00:00"
        end_iso = f"{end_date}T23:59:59"
        url = _supabase_rest_url("manager_transactions")
        params = {"select": "*", "published_at": [f"gte.{start_iso}", f"lte.{end_iso}"], "order": "published_at.desc"}
        with _http_client() as client:
            response = client.get(url, headers=_supabase_headers(), params=params)
            response.raise_for_status()
            data = response.json() or []
        return pd.DataFrame(data)
    except Exception as exc:
        st.error(f"Nepavyko nuskaityti manager_transactions lentelės: {exc}")
        return pd.DataFrame()


def load_manager_transactions_from_db(start_date, end_date) -> pd.DataFrame:
    if load_manager_transactions_df is not None:
        return load_manager_transactions_df(start_date, end_date)
    return _load_manager_transactions_df_fallback(start_date, end_date)


def _infer_issuer_from_news_text(title: str = "", content: str = "") -> str:
    """Bando nustatyti emitentą iš CRIB antraštės / turinio, jei market_news company tuščias."""
    text = f"{title or ''} {content or ''}"
    key_text = _issuer_norm_key(text)
    if not key_text:
        return ""

    lookup = _load_issuer_lookup_from_market_issuers()
    best = ""
    best_len = 0
    for key, canonical in lookup.items():
        if not key or len(key) < 3:
            continue
        if re.search(rf"(?:^|\s){re.escape(key)}(?:\s|$)", key_text):
            if len(key) > best_len:
                best = canonical
                best_len = len(key)
    return best


def load_crib_news_df(start_date, end_date) -> pd.DataFrame:
    df = load_news_df("crib", start_date, end_date)
    if df is None or df.empty:
        return pd.DataFrame(columns=["issuer", "issuer_norm", "category", "title", "published_at", "crib_url", "content"])
    df = df.copy()

    for col in ["company", "category", "title", "published_at", "url", "content"]:
        if col not in df.columns:
            df[col] = ""

    df["category"] = df["category"].fillna("").astype(str).str.strip()
    df["title"] = df["title"].fillna("").astype(str).str.strip()
    df["published_at"] = df.get("published_at", None)
    df["crib_url"] = df["url"].fillna("").astype(str).str.strip()
    df["content"] = df["content"].fillna("").astype(str).str.strip()

    df["issuer"] = df["company"].fillna("").astype(str).str.strip().apply(_canonical_issuer_name)

    # Jei Supabase market_news.company tuščias arba nesutampa su market_issuers,
    # emitentą bandome ištraukti iš CRIB antraštės / turinio pagal market_issuers žodyną.
    missing_issuer = df["issuer"].fillna("").astype(str).str.strip().eq("")
    if missing_issuer.any():
        df.loc[missing_issuer, "issuer"] = df.loc[missing_issuer].apply(
            lambda r: _infer_issuer_from_news_text(r.get("title", ""), r.get("content", "")),
            axis=1,
        )

    df["issuer"] = df["issuer"].fillna("").astype(str).str.strip().apply(_canonical_issuer_name)
    df["issuer_norm"] = df["issuer"].apply(_issuer_norm_key)

    return df[["issuer", "issuer_norm", "category", "title", "published_at", "crib_url", "content"]]


ANNUAL_PATTERNS = [
    r"\bmetin",
    r"\bmetų\s+ataskait",
    r"\bmetines?\s+finansin",
    r"\baudituot",
    r"\baudited\b",
    r"\bannual\s+(?:information|report|financial)",
]

HALF_YEAR_PATTERNS = [
    r"\b6\s*m[ėe]n",
    r"\b6\s*m[ėe]nes",
    r"\bšešių\s+m[ėe]nesių\b",
    r"\bsesiu\s+menesiu\b",
    r"\bpusme",
    r"\bi\s+pusme",
    r"\b1\s+pusme",
    r"\bh1\b",
    r"\bhalf[- ]year\b",
    r"\bfirst\s+half\b",
    r"\bsemi[- ]annual\b",
    r"\b6\s*months\b",
    r"\bsix[- ]month",
    r"\bsix\s+months\b",
]

QUARTER_REPORT_PATTERNS = [
    r"\b3\s*m[ėe]n",
    r"\b3\s*m[ėe]nes",
    r"\btrijų\s+m[ėe]nesių\b",
    r"\btriju\s+menesiu\b",
    r"\b9\s*m[ėe]n",
    r"\b9\s*m[ėe]nes",
    r"\bdevynių\s+m[ėe]nesių\b",
    r"\bdevyniu\s+menesiu\b",
    r"\bq1\b",
    r"\bq3\b",
    r"\bi\s+ketv",
    r"\biii\s+ketv",
    r"\bpirm[ao]\s+ketv",
    r"\btre[cč]i[ao]\s+ketv",
    r"\bthree\s+months\b",
    r"\bnine\s+months\b",
]

NON_REPORT_PATTERNS = [
    r"\bpreliminar",
    r"\bprognoz",
    r"\bdividend",
    r"\bprezentacij",
    r"\bpresentation\b",
]


def _norm_text(x) -> str:
    return str(x or "").lower().strip()


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text or "", flags=re.IGNORECASE) for p in patterns)


def _is_annual_category(category: str) -> bool:
    category = _norm_text(category)
    return bool(re.search(r"\bmetin|\bannual\s+information", category, flags=re.I))


def _is_interim_category(category: str) -> bool:
    category = _norm_text(category)
    return bool(re.search(r"\btarpin|\binterim\s+information", category, flags=re.I))


def _classify_financial_report(row) -> str:
    """
    CRIB kategorija yra pagrindinis šaltinis:
    - „Metinė informacija“ / „Annual information“ => metinė ataskaita;
    - „Tarpinė informacija“ / „Interim information“ => gali būti 3 mėn., 6 mėn. arba 9 mėn.

    DPL patikrai įtraukiame tik metines ir 6 mėn. / pusmečio ataskaitas.
    Todėl tarpinės informacijos atveju papildomai tikriname antraštę ir turinį,
    kad neatimtume 3 mėn. arba 9 mėn. pranešimų kaip pusmečio ataskaitų.
    """
    category = _norm_text(row.get("category", ""))
    title = _norm_text(row.get("title", ""))
    content = _norm_text(row.get("content", ""))
    text = f"{category} {title} {content[:1500]}"

    if _matches_any(text, NON_REPORT_PATTERNS):
        return ""

    if _is_annual_category(category) or _matches_any(text, ANNUAL_PATTERNS):
        return "Metinė"

    if _is_interim_category(category):
        if _matches_any(text, QUARTER_REPORT_PATTERNS):
            return ""
        if _matches_any(text, HALF_YEAR_PATTERNS):
            return "Pusmečio / 6 mėn."
        return ""

    if _matches_any(text, HALF_YEAR_PATTERNS):
        return "Pusmečio / 6 mėn."

    return ""


def prepare_dpl_periods_df(news_df: pd.DataFrame) -> pd.DataFrame:
    if news_df is None or news_df.empty:
        return pd.DataFrame()
    df = news_df.copy()
    for col in ["issuer", "issuer_norm", "category", "title", "published_at", "crib_url", "content"]:
        if col not in df.columns:
            df[col] = ""
    df["report_published_date"] = pd.to_datetime(df["published_at"], errors="coerce", utc=True).dt.date
    for col in ["issuer", "issuer_norm", "category", "title", "crib_url", "content"]:
        df[col] = df[col].fillna("").astype(str).str.strip()
    df["dpl_report_type"] = df.apply(_classify_financial_report, axis=1)
    df = df[(df["issuer"] != "") & df["report_published_date"].notna() & (df["dpl_report_type"] != "")].copy()
    if df.empty:
        return pd.DataFrame()
    df["dpl_start_date"] = df["report_published_date"].apply(lambda x: x - timedelta(days=30))
    df["dpl_end_date"] = df["report_published_date"]
    return df[["issuer", "issuer_norm", "dpl_report_type", "report_published_date", "dpl_start_date", "dpl_end_date", "title", "category", "crib_url"]].drop_duplicates()


def _issuer_key(value) -> str:
    return _issuer_norm_key(_canonical_issuer_name(value))


def add_dpl_check_to_transactions(transactions_df: pd.DataFrame, dpl_periods_df: pd.DataFrame) -> pd.DataFrame:
    """
    Prideda DPL patikrą prie vadovų sandorių.

    Pataisyta: pandas naujesnėse versijose nebeleidžia į datetime64 stulpelį
    tiesiogiai įrašyti datetime.date reikšmės. Todėl DPL datos laikomos kaip
    object/date, o ne kaip datetime64[ns]. Tai pašalina klaidą:
    TypeError: Invalid value 'YYYY-MM-DD' for dtype datetime64[ns].
    """
    if transactions_df is None or transactions_df.empty:
        return pd.DataFrame()

    df = transactions_df.copy()

    # Šiuos stulpelius sąmoningai kuriame kaip object, nes vėliau įrašome datetime.date.
    df["is_dpl_period"] = False
    df["dpl_report_type"] = ""
    df["dpl_report_date"] = pd.Series([None] * len(df), index=df.index, dtype="object")
    df["dpl_start_date"] = pd.Series([None] * len(df), index=df.index, dtype="object")
    df["dpl_end_date"] = pd.Series([None] * len(df), index=df.index, dtype="object")
    df["dpl_days_to_report"] = pd.Series([pd.NA] * len(df), index=df.index, dtype="object")
    df["dpl_report_title"] = ""
    df["dpl_report_url"] = ""

    def _to_date_obj(value):
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        try:
            return pd.to_datetime(value, errors="coerce").date()
        except Exception:
            return value

    if dpl_periods_df is not None and not dpl_periods_df.empty:
        periods = dpl_periods_df.copy()

        periods["issuer_key"] = periods["issuer"].apply(_issuer_key)
        df["issuer_key"] = df["issuer"].apply(_issuer_key)

        # Užtikriname vienodą datų tipą palyginimui.
        for col in ["dpl_start_date", "dpl_end_date", "report_published_date"]:
            if col in periods.columns:
                periods[col] = periods[col].apply(_to_date_obj)

        for idx, row in df.iterrows():
            issuer = row.get("issuer_key", "")
            trade_date = _to_date_obj(row.get("transaction_date_dt"))

            if not issuer or trade_date is None:
                continue

            matches = periods[
                (periods["issuer_key"] == issuer)
                & (periods["dpl_start_date"].notna())
                & (periods["dpl_end_date"].notna())
                & (periods["dpl_start_date"] <= trade_date)
                & (periods["dpl_end_date"] >= trade_date)
            ].copy()

            if matches.empty:
                continue

            matches["days_to_report"] = matches["report_published_date"].apply(
                lambda x: (x - trade_date).days if x is not None else pd.NA
            )

            match = matches.sort_values("days_to_report").iloc[0]

            df.at[idx, "is_dpl_period"] = True
            df.at[idx, "dpl_report_type"] = str(match.get("dpl_report_type", "") or "")
            df.at[idx, "dpl_report_date"] = _to_date_obj(match.get("report_published_date"))
            df.at[idx, "dpl_start_date"] = _to_date_obj(match.get("dpl_start_date"))
            df.at[idx, "dpl_end_date"] = _to_date_obj(match.get("dpl_end_date"))
            df.at[idx, "dpl_days_to_report"] = match.get("days_to_report", pd.NA)
            df.at[idx, "dpl_report_title"] = str(match.get("title", "") or "")
            df.at[idx, "dpl_report_url"] = str(match.get("crib_url", "") or "")

        df.drop(columns=["issuer_key"], inplace=True, errors="ignore")

    df["DPL"] = df["is_dpl_period"].apply(lambda x: "Taip" if x else "Ne")
    df["DPL tipas"] = df["dpl_report_type"].fillna("")
    df["DPL pradžia"] = df["dpl_start_date"]
    df["DPL pabaiga"] = df["dpl_end_date"]
    df["Ataskaitos paskelbimo data"] = df["dpl_report_date"]
    df["DPL dienų iki ataskaitos"] = df["dpl_days_to_report"]
    df["Susijusi ataskaita"] = df["dpl_report_title"].fillna("")
    df["Ataskaitos nuoroda"] = df["dpl_report_url"].fillna("")

    df["DPL paaiškinimas"] = df.apply(
        lambda r: (
            f"Sandoris sudarytas DPL laikotarpiu: {r['dpl_days_to_report']} k. d. iki "
            f"{str(r['dpl_report_type']).lower()} ataskaitos paskelbimo "
            f"({r['dpl_report_date']}). DPL: {r['dpl_start_date']}–{r['dpl_end_date']}."
            if r["is_dpl_period"]
            else "Sandorio data nepatenka į identifikuotus metinės arba pusmečio / 6 mėn. ataskaitos DPL laikotarpius."
        ),
        axis=1,
    )

    return df

def prepare_manager_transactions_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    # Čia atliekamas pagrindinis pataisymas: techniniai nepavykusio PDF
    # nuskaitymo įrašai nerodomi ataskaitoje ir nepatenka į santraukas.
    df = _filter_hidden_manager_report_rows(df)
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    for col in ["published_at", "transaction_date"]:
        if col not in df.columns:
            df[col] = None
    published_dt = pd.to_datetime(df["published_at"], errors="coerce", utc=True)
    transaction_dt = pd.to_datetime(df["transaction_date"], errors="coerce")
    df["published_date"] = published_dt.dt.date
    df["transaction_date_dt"] = transaction_dt.dt.date
    df["days_to_publish"] = (pd.to_datetime(df["published_date"], errors="coerce") - pd.to_datetime(df["transaction_date_dt"], errors="coerce")).dt.days
    df["is_late_notification"] = df["days_to_publish"].apply(lambda x: bool(pd.notna(x) and x > 3))
    for col in ["issuer", "person_name", "person_role", "isin", "instrument", "transaction_type", "venue", "parse_status", "price_quantity_note"]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str).str.strip()
    df["issuer"] = df["issuer"].apply(_canonical_issuer_name)
    for col in ["price", "quantity"]:
        if col not in df.columns:
            df[col] = None
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["transaction_value"] = df["price"] * df["quantity"]
    df = _remove_manager_duplicates_for_display(df)
    return df


def _apply_multiselect_filter(df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    if col not in df.columns:
        return df
    values = sorted([x for x in df[col].dropna().unique() if str(x).strip()])
    selected = st.multiselect(label, values, key=f"mgr_filter_{col}")
    if selected:
        return df[df[col].isin(selected)]
    return df



def _format_dpl_period(row) -> str:
    """Suformuoja trumpą DPL laikotarpio tekstą lentelei."""
    if row is None or row.empty:
        return ""
    start = row.get("dpl_start_date", "")
    end = row.get("dpl_end_date", "")
    report_date = row.get("report_published_date", "")
    title = str(row.get("title", "") or "").strip()

    def fmt(x):
        try:
            if pd.isna(x):
                return ""
        except Exception:
            pass
        try:
            return pd.to_datetime(x).date().isoformat()
        except Exception:
            return str(x or "")

    period = f"{fmt(start)} – {fmt(end)}".strip(" –")
    if report_date:
        period = f"{period} (ataskaita: {fmt(report_date)})" if period else f"Ataskaita: {fmt(report_date)}"
    if title:
        period = f"{period}\n{title}" if period else title
    return period


def build_dpl_dates_summary_df(dpl_periods_df: pd.DataFrame) -> pd.DataFrame:
    """
    Paruošia įmonių DPL datų lentelę:
    - įmonės pavadinimas;
    - paskutinis 6 mėn. / pusmečio DPL laikotarpis;
    - paskutinis metinės ataskaitos DPL laikotarpis.
    """
    columns = ["Įmonė", "6 mėn. DPL laikotarpis", "Metų DPL laikotarpis"]
    if dpl_periods_df is None or dpl_periods_df.empty:
        return pd.DataFrame(columns=columns)

    df = dpl_periods_df.copy()
    for col in ["issuer", "dpl_report_type", "report_published_date", "dpl_start_date", "dpl_end_date", "title"]:
        if col not in df.columns:
            df[col] = ""

    df["issuer"] = df["issuer"].fillna("").astype(str).str.strip()
    df = df[df["issuer"] != ""].copy()
    if df.empty:
        return pd.DataFrame(columns=columns)

    df["report_published_date_sort"] = pd.to_datetime(df["report_published_date"], errors="coerce")
    df = df.sort_values(["issuer", "report_published_date_sort"], ascending=[True, False])

    rows = []
    for issuer, g in df.groupby("issuer", sort=True):
        g = g.copy()
        half = g[g["dpl_report_type"].fillna("").astype(str).str.contains("6|pusme", case=False, regex=True)]
        annual = g[g["dpl_report_type"].fillna("").astype(str).str.contains("metin", case=False, regex=True)]

        half_text = _format_dpl_period(half.iloc[0]) if not half.empty else ""
        annual_text = _format_dpl_period(annual.iloc[0]) if not annual.empty else ""

        rows.append({
            "Įmonė": issuer,
            "6 mėn. DPL laikotarpis": half_text,
            "Metų DPL laikotarpis": annual_text,
        })

    return pd.DataFrame(rows, columns=columns)


def show_dpl_dates_table(dpl_periods_df: pd.DataFrame):
    """Parodo DPL datų lentelę vadovų sandorių ataskaitoje."""
    st.subheader("DPL laikotarpiai pagal įmones")
    dpl_dates_df = build_dpl_dates_summary_df(dpl_periods_df)
    if dpl_dates_df.empty:
        st.info("DPL laikotarpių nerasta pagal pasirinktą vadovų sandorių laikotarpį.")
    else:
        st.dataframe(dpl_dates_df, use_container_width=True, hide_index=True)



# ------------------------------------------------------------
# Vadovų sandorių dublikatų valymas
# ------------------------------------------------------------

def _dup_norm_text(value) -> str:
    """Normalizuoja tekstą dublikatų palyginimui."""
    s = str(value or "").strip().lower()
    repl = str.maketrans({"ą":"a","č":"c","ę":"e","ė":"e","į":"i","š":"s","ų":"u","ū":"u","ž":"z"})
    s = s.translate(repl)
    s = re.sub(r"\b(ab|uab|as|akcine bendrove|uzdaroji akcine bendrove)\b", " ", s)
    s = s.replace("paprastoji vardine akcija", "akcija")
    s = s.replace("paprastosios vardines akcijos", "akcija")
    s = s.replace("ordinary registered shares", "akcija")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _dup_date(value) -> str:
    try:
        dt = pd.to_datetime(value, errors="coerce")
        if pd.notna(dt):
            return dt.date().isoformat()
    except Exception:
        pass
    return str(value or "").strip()[:10]


def _dup_number(value, decimals: int = 6) -> str:
    try:
        if value is None or pd.isna(value):
            return ""
        val = float(value)
        if abs(val) < 1e-12:
            # Nulis vadovų sandoriuose dažnai reiškia opcioną / paveldėjimą;
            # paliekame kaip tikrą reikšmę, o ne kaip tuščią.
            return "0"
        return f"{val:.{decimals}f}".rstrip("0").rstrip(".")
    except Exception:
        s = str(value or "").replace(" ", "").replace(",", ".").strip()
        return s


def _row_has_price_quantity(row) -> bool:
    try:
        price_ok = row.get("price") is not None and not pd.isna(row.get("price"))
    except Exception:
        price_ok = bool(str(row.get("price") or "").strip())
    try:
        quantity_ok = row.get("quantity") is not None and not pd.isna(row.get("quantity"))
    except Exception:
        quantity_ok = bool(str(row.get("quantity") or "").strip())
    return bool(price_ok and quantity_ok)


def _manager_duplicate_score(row) -> float:
    """Kuo didesnis balas, tuo įrašas laikomas geresniu dublikato variante."""
    score = 0.0
    important_cols = [
        "issuer", "person_name", "person_role", "transaction_date", "transaction_date_dt",
        "isin", "instrument", "transaction_type", "price", "quantity", "venue",
        "published_at", "published_date", "crib_url", "pdf_url", "raw_text",
    ]
    for col in important_cols:
        val = row.get(col)
        try:
            empty = val is None or pd.isna(val) or str(val).strip() == ""
        except Exception:
            empty = str(val or "").strip() == ""
        if not empty:
            score += 1

    if _row_has_price_quantity(row):
        score += 40
    if _looks_like_valid_isin(row.get("isin")):
        score += 10

    pdf_url = str(row.get("pdf_url") or "").lower()
    if "crib.lt" in pdf_url and "viewattachment" in pdf_url:
        score += 12
    if "globenewswire.com" in pdf_url:
        score -= 8

    status = str(row.get("parse_status") or "").lower()
    if status in {"parsed_mar_form", "repaired_empty_fields"}:
        score += 8
    elif status in {"parsed_incomplete", "repaired_partial_fields"}:
        score += 2
    elif "error" in status or "empty" in status:
        score -= 10

    raw_len = len(str(row.get("raw_text") or ""))
    score += min(raw_len / 1000.0, 6.0)

    try:
        if row.get("id") is not None and not pd.isna(row.get("id")):
            # Jei viskas vienoda, paliekame naujesnį / vėlesnį įrašą.
            score += min(float(row.get("id")) / 1000000.0, 1.0)
    except Exception:
        pass
    return score


def _manager_duplicate_key_strict(row) -> tuple:
    return (
        _dup_norm_text(_canonical_issuer_name(row.get("issuer"))),
        _dup_norm_text(row.get("person_name")),
        _dup_date(row.get("transaction_date_dt") or row.get("transaction_date")),
        str(row.get("isin") or "").strip().upper(),
        _dup_norm_text(row.get("transaction_type")),
        _dup_norm_text(row.get("instrument")),
        _dup_number(row.get("quantity"), decimals=0),
        _dup_number(row.get("price"), decimals=6),
        _dup_norm_text(row.get("venue")),
    )


def _manager_duplicate_key_loose(row) -> tuple:
    """Platesnis raktas silpniems dublikatams.

    Kai tas pats CRIB pranešimas turi ir gerą CRIB attachment įrašą, ir
    prastesnį Globenewswire veidrodinį įrašą, prastesniame variante dažnai
    skiriasi `transaction_type`, `instrument` arba `venue` tekstai. Pvz.
    vienoje eilutėje yra „Įsigijimas“, kitoje – ilgas tekstas
    „Finansinių priemonių įgijimas paveldėjimo būdu“, o vietoje dar prisiklijuoja
    pareigos. Todėl plataus rakto sąmoningai neribojame pagal pusę / vietą /
    priemonę. Jį taikome tik tada, kai grupėje yra pilnas ir nepilnas įrašas,
    todėl tikri keli sandoriai su skirtingais kiekiais nėra trinami.
    """
    crib = str(row.get("crib_url") or "").strip().lower()
    return (
        crib,
        _dup_norm_text(_canonical_issuer_name(row.get("issuer"))),
        _dup_norm_text(row.get("person_name")),
        _dup_date(row.get("transaction_date_dt") or row.get("transaction_date")),
        str(row.get("isin") or "").strip().upper(),
    )


def _duplicate_ids_from_rows(rows: list[dict]) -> list[int]:
    """Grąžina ID, kuriuos galima saugiai trinti kaip dublikatus."""
    if not rows:
        return []

    # Pirmas etapas: visiškai tas pats sandoris pagal faktinius laukus.
    by_strict = {}
    for r in rows:
        key = _manager_duplicate_key_strict(r)
        if not any(key):
            continue
        by_strict.setdefault(key, []).append(r)

    delete_ids = set()
    for group in by_strict.values():
        if len(group) <= 1:
            continue
        ranked = sorted(group, key=_manager_duplicate_score, reverse=True)
        for r in ranked[1:]:
            if r.get("id") is not None:
                try:
                    delete_ids.add(int(r.get("id")))
                except Exception:
                    pass

    # Antras etapas: tas pats CRIB pranešimas ir sandorio tapatybė, bet viena eilutė nepilna.
    remaining = [r for r in rows if r.get("id") is not None and pd.notna(r.get("id")) and int(r.get("id")) not in delete_ids]
    by_loose = {}
    for r in remaining:
        key = _manager_duplicate_key_loose(r)
        # Be crib_url tokio plataus palyginimo netaikome, kad nesutrintume realių atskirų sandorių.
        if not key[0] or not key[1] or not key[2] or not key[3]:
            continue
        by_loose.setdefault(key, []).append(r)

    for group in by_loose.values():
        if len(group) <= 1:
            continue
        complete = [r for r in group if _row_has_price_quantity(r)]
        incomplete = [r for r in group if not _row_has_price_quantity(r)]
        if complete and incomplete:
            # Paliekame pilnus įrašus. Triname tik nepilnus veidrodinius įrašus.
            for r in incomplete:
                try:
                    delete_ids.add(int(r.get("id")))
                except Exception:
                    pass
        else:
            # Jei visi vienodai pilni arba visi nepilni, paliekame geriausią.
            ranked = sorted(group, key=_manager_duplicate_score, reverse=True)
            for r in ranked[1:]:
                try:
                    delete_ids.add(int(r.get("id")))
                except Exception:
                    pass

    return sorted(delete_ids)


def _remove_manager_duplicates_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Paslepia dublikatus ataskaitoje, net jei jie dar fiziškai yra DB."""
    if df is None or df.empty:
        return pd.DataFrame()
    if "id" not in df.columns:
        return df

    rows = df.to_dict("records")
    delete_ids = set(_duplicate_ids_from_rows(rows))
    if not delete_ids:
        return df
    return df[~df["id"].apply(lambda x: int(x) in delete_ids if pd.notna(x) else False)].copy().reset_index(drop=True)


def delete_duplicate_manager_transactions(limit: int = 3000) -> dict:
    """Fiziškai ištrina dublikatus iš Supabase manager_transactions lentelės."""
    stats = {"checked": 0, "duplicates_found": 0, "deleted": 0, "errors": 0, "error_messages": []}
    try:
        _headers, _url, _client = _supabase_client_parts()
        url = _url("manager_transactions")
        params = {
            "select": "id,published_at,pdf_url,pdf_name,crib_url,crib_title,issuer,person_name,person_role,isin,instrument,transaction_type,price,quantity,transaction_date,venue,raw_text,parse_status,price_quantity_note",
            "order": "id.desc",
            "limit": str(limit),
        }
        with _client() as client:
            resp = client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            rows = resp.json() or []

        stats["checked"] = len(rows)
        duplicate_ids = _duplicate_ids_from_rows(rows)
        stats["duplicates_found"] = len(duplicate_ids)

        for row_id in duplicate_ids:
            try:
                if _delete_manager_transaction_by_id(int(row_id)):
                    stats["deleted"] += 1
            except Exception as exc:
                stats["errors"] += 1
                if len(stats["error_messages"]) < 10:
                    stats["error_messages"].append(f"id={row_id}: {exc}")
    except Exception as exc:
        stats["errors"] += 1
        stats["error_messages"].append(str(exc))
    return stats


def _show_summary_cards(df: pd.DataFrame):
    total = len(df)
    issuers = df["issuer"].replace("", pd.NA).dropna().nunique()
    persons = df["person_name"].replace("", pd.NA).dropna().nunique()
    late = int(df["is_late_notification"].sum())
    dpl = int(df["is_dpl_period"].sum())
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Pranešimų / PDF", total)
    c2.metric("Emitentų", issuers)
    c3.metric("Asmenų", persons)
    c4.metric("Vėluojančių >3 d.", late)
    c5.metric("Per DPL", dpl)


def _style_dpl_value(value):
    """Spalvina DPL stulpelį detalioje vadovų sandorių lentelėje."""
    v = str(value or "").strip().lower()
    if v == "taip":
        return "background-color: #f8d7da; color: #721c24; font-weight: 700;"
    if v == "ne":
        return "background-color: #d4edda; color: #155724; font-weight: 700;"
    return ""


def _style_delay_value(value):
    """Spalvina vėlavimo dienų stulpelį detalioje vadovų sandorių lentelėje."""
    try:
        if pd.isna(value):
            return ""
        days = float(value)
    except Exception:
        return ""

    if days > 3:
        return "background-color: #f8d7da; color: #721c24; font-weight: 700;"
    return "background-color: #d4edda; color: #155724; font-weight: 700;"


def _prepare_manager_transactions_display_df(df: pd.DataFrame) -> pd.DataFrame:
    """Paruošia detalios vadovų sandorių lentelės rodymo / eksporto DataFrame.

    Pageidaujama pradžios tvarka:
    Įmonės pavadinimas, Asmuo, Pranešimo data, Sandorio data,
    Pranešta per d., Pavadinimas, Pusė, Kiekis, Kaina, Vertė, Vieta, DPL.
    Visi kiti turimi parametrai paliekami lentelės gale.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    first_cols = [
        "issuer",
        "person_name",
        "published_date",
        "transaction_date_dt",
        "days_to_publish",
        "instrument",
        "transaction_type",
        "quantity",
        "price",
        "transaction_value",
        "venue",
        "DPL",
    ]

    # Šie stulpeliai rodomi po pagrindinių. Jei ateityje atsiras naujų DB laukų,
    # jie automatiškai bus pridėti dar toliau per extra_cols.
    preferred_tail_cols = [
        "person_role",
        "DPL tipas",
        "Susijusi ataskaita",
        "isin",
        "Ataskaitos paskelbimo data",
        "DPL pradžia",
        "DPL pabaiga",
        "DPL dienų iki ataskaitos",
        "Ataskaitos nuoroda",
        "lei",
        "pdf_name",
        "pdf_url",
        "crib_url",
        "price_quantity_note",
        "parse_status",
    ]

    # Techninius / tarpinius stulpelius, kurie neturi būti rodomi lentelės gale, paslepiame.
    hidden_cols = {
        "raw_text",
        "published_at",
        "transaction_date",
        "published_at_dt",
        "transaction_date_dt_raw",
        "is_late_notification",
        "is_dpl_period",
        "dpl_report_type",
        "dpl_report_date",
        "dpl_start_date",
        "dpl_end_date",
        "dpl_days_to_report",
        "dpl_report_title",
        "dpl_report_url",
        "DPL paaiškinimas",
    }

    ordered_cols = []
    for col in first_cols + preferred_tail_cols:
        if col in df.columns and col not in ordered_cols and col not in hidden_cols:
            ordered_cols.append(col)

    extra_cols = [
        col for col in df.columns
        if col not in ordered_cols and col not in hidden_cols
    ]
    detail_cols = ordered_cols + extra_cols

    display_df = df[detail_cols].copy()
    display_df = display_df.rename(columns={
        "issuer": "Įmonės pavadinimas",
        "person_name": "Asmuo",
        "published_date": "Pranešimo data",
        "transaction_date_dt": "Sandorio data",
        "days_to_publish": "Pranešta per d.",
        "instrument": "Pavadinimas",
        "transaction_type": "Pusė",
        "quantity": "Kiekis",
        "price": "Kaina",
        "transaction_value": "Vertė",
        "venue": "Vieta",
        "person_role": "Pareigos",
        "isin": "ISIN",
        "lei": "LEI",
        "pdf_name": "PDF pavadinimas",
        "pdf_url": "PDF nuoroda",
        "crib_url": "CRIB nuoroda",
        "price_quantity_note": "Pastaba dėl kainos / kiekio",
        "parse_status": "Apdorojimo statusas",
    })

    return display_df


def _show_tables(df: pd.DataFrame):
    st.subheader("1. Detali vadovų sandorių lentelė")

    display_df = _prepare_manager_transactions_display_df(df)

    format_map = {}
    if "Kaina" in display_df.columns:
        format_map["Kaina"] = "{:.4f}"
    if "Kiekis" in display_df.columns:
        format_map["Kiekis"] = "{:.0f}"
    if "Vertė" in display_df.columns:
        format_map["Vertė"] = "{:,.2f}"
    if "Pranešta per d." in display_df.columns:
        format_map["Pranešta per d."] = "{:.0f}"

    # Tą patį DataFrame išsaugome CSV atsisiuntimui, kad eksportas turėtų
    # tokią pačią stulpelių tvarką kaip matoma lentelė.
    st.session_state["manager_transactions_display_df"] = display_df.copy()

    styler = display_df.style
    if format_map:
        styler = styler.format(format_map, na_rep="")

    # pandas >= 2.1 Styler.applymap nebepalaikomas, todėl naudojame Styler.map.
    # Paliekame atsarginį variantą senesnėms pandas versijoms.
    if "DPL" in display_df.columns:
        if hasattr(styler, "map"):
            styler = styler.map(_style_dpl_value, subset=["DPL"])
        else:
            styler = styler.applymap(_style_dpl_value, subset=["DPL"])
    if "Pranešta per d." in display_df.columns:
        if hasattr(styler, "map"):
            styler = styler.map(_style_delay_value, subset=["Pranešta per d."])
        else:
            styler = styler.applymap(_style_delay_value, subset=["Pranešta per d."])

    st.dataframe(styler, use_container_width=True, hide_index=True)

    st.subheader("2. Santrauka pagal asmenį")
    person_summary = (
        df.groupby(["issuer", "person_name"], dropna=False)
        .agg(
            pranesimu_sk=("pdf_url", "count"),
            sandorio_bendra_verte=("transaction_value", "sum"),
            veluojanciu_sk=("is_late_notification", "sum"),
            dpl_sandoriu_sk=("is_dpl_period", "sum"),
            vid_dienu_iki_pranesimo=("days_to_publish", "mean"),
        )
        .reset_index()
        .sort_values(["dpl_sandoriu_sk", "pranesimu_sk", "sandorio_bendra_verte"], ascending=[False, False, False])
    )
    st.dataframe(person_summary, use_container_width=True, hide_index=True)


# ------------------------------------------------------------
# Streamlit puslapis
# ------------------------------------------------------------

def show_manager_transactions_page():
    st.markdown(
        """
        <div class="hero-card">
            <div class="hero-inner">
                <div class="hero-icon">👔</div>
                <div>
                    <h1 class="hero-title">Vadovų sandoriai</h1>
                    <div class="hero-text">
                        CRIB kategorijos „Pranešimai apie vadovų sandorius“ PDF dokumentai.
                        Lentelėje papildomai tikrinama, ar sandoris vyko DPL laikotarpiu
                        prieš metinės arba pusmečio / 6 mėn. ataskaitos paskelbimą.
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)
    st.caption("Vadovų sandorių modulis: dublikatų valymo versija 2026-07-03b")

    with st.sidebar:
        st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
        st.markdown('<div class="sidebar-card-title">👔 Vadovų sandoriai</div>', unsafe_allow_html=True)
        manager_start_date = st.date_input("Pranešimo data nuo", value=date.today() - timedelta(days=30), key="manager_start_date")
        manager_end_date = st.date_input("Pranešimo data iki", value=date.today(), key="manager_end_date")
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-card-subtitle">Atnaujina vadovų sandorių PDF iš CRIB pranešimų, kurie jau yra market_news lentelėje.</div>',
            unsafe_allow_html=True,
        )
        manager_update_days = st.number_input("Tikrinti paskutines dienas", min_value=7, max_value=180, value=45, step=1, key="manager_update_days")
        manager_update_btn = st.button("🔄 Atnaujinti vadovų sandorius", use_container_width=True, key="manager_transactions_update_btn")
        latest_recalc_btn = st.button("🔁 Perskaičiuoti paskutinį pranešimą", use_container_width=True, key="manager_latest_recalc_btn")
        duplicate_cleanup_btn = st.button("🧽 Ištrinti dublikatus DB", use_container_width=True, key="manager_duplicate_cleanup_btn")
        repair_bad_btn = st.button("🔧 Sutvarkyti blogai nuskaitytus PDF", use_container_width=True, key="manager_repair_bad_btn")
        cleanup_hidden_btn = st.button("🧹 Ištrinti techninius tuščius įrašus", use_container_width=True, key="manager_cleanup_hidden_btn")
        repair_limit = st.number_input("Blogų PDF / dublikatų limitas", min_value=10, max_value=5000, value=1000, step=10, key="manager_repair_limit")
        st.markdown("</div>", unsafe_allow_html=True)

    if manager_update_btn:
        try:
            with st.spinner("Tikrinami paskutiniai CRIB vadovų sandorių pranešimai ir PDF..."):
                stats = update_manager_transactions_from_recent_crib(days_back=int(manager_update_days), max_messages=50, headless=True, progress=None)
            st.success(
                "Vadovų sandoriai atnaujinti: "
                f"rasta CRIB pranešimų {stats.get('manager_messages_found', 0)}, "
                f"apdorota {stats.get('manager_messages_processed', 0)}, "
                f"naujai įrašyta sandorių/PDF {stats.get('manager_transactions_saved', 0)}, "
                f"klaidų {stats.get('manager_transactions_errors', 0)}."
            )
            st.rerun()
        except Exception as exc:
            st.error("Nepavyko atnaujinti vadovų sandorių.")
            st.exception(exc)
            st.stop()

    if latest_recalc_btn:
        try:
            with st.spinner("Perskaičiuojamas paskutinis vadovų sandorių pranešimas..."):
                stats = recalc_latest_manager_notice(headless=True, progress=None)
            st.success(
                "Paskutinis pranešimas perskaičiuotas: "
                f"rastas={stats.get('notice_found')}, "
                f"ištrinta {stats.get('deleted', 0)}, "
                f"įrašyta {stats.get('inserted', 0)}, "
                f"klaidų {stats.get('errors', 0)}."
            )
            st.rerun()
        except Exception as exc:
            st.error("Nepavyko perskaičiuoti paskutinio pranešimo.")
            st.exception(exc)
            st.stop()

    if duplicate_cleanup_btn:
        try:
            with st.spinner("Ieškomi ir trinami vadovų sandorių dublikatai DB..."):
                stats = delete_duplicate_manager_transactions(limit=int(repair_limit))
            if stats.get("errors", 0):
                st.warning(
                    "Dublikatų valymas baigtas su klaidomis: "
                    f"patikrinta {stats.get('checked', 0)}, "
                    f"rasta dublikatų {stats.get('duplicates_found', 0)}, "
                    f"ištrinta {stats.get('deleted', 0)}, "
                    f"klaidų {stats.get('errors', 0)}."
                )
                if stats.get("error_messages"):
                    st.code("\n".join(stats.get("error_messages", [])[:10]))
            else:
                st.success(
                    "Dublikatų valymas baigtas: "
                    f"patikrinta {stats.get('checked', 0)}, "
                    f"rasta dublikatų {stats.get('duplicates_found', 0)}, "
                    f"ištrinta {stats.get('deleted', 0)}."
                )
            st.rerun()
        except Exception as exc:
            st.error("Nepavyko ištrinti dublikatų.")
            st.exception(exc)
            st.stop()

    if repair_bad_btn:
        try:
            with st.spinner("Taisomi blogai nuskaityti PDF įrašai..."):
                stats = repair_bad_manager_transactions(limit=int(repair_limit), progress=None)
            st.success(
                "Blogų PDF taisymas baigtas: "
                f"rasta {stats.get('bad_found', 0)}, "
                f"sutvarkyta {stats.get('repaired', 0)}, "
                f"ištrinta tuščių dublikatų {stats.get('deleted_duplicates', 0)}, "
                f"dalinai {stats.get('partial', 0)}, nepakeista {stats.get('unchanged', 0)}, nepavyko {stats.get('failed', 0)}."
            )
            st.rerun()
        except Exception as exc:
            st.error("Nepavyko sutvarkyti blogai nuskaitytų PDF.")
            st.exception(exc)
            st.stop()

    if cleanup_hidden_btn:
        try:
            with st.spinner("Trinami techniniai tušti vadovų sandorių įrašai..."):
                stats = delete_hidden_manager_report_rows(limit=int(repair_limit))
            st.success(
                "Techniniai tušti įrašai sutvarkyti: "
                f"rasta {stats.get('found', 0)}, "
                f"ištrinta {stats.get('deleted', 0)}, "
                f"klaidų {stats.get('errors', 0)}."
            )
            st.rerun()
        except Exception as exc:
            st.error("Nepavyko ištrinti techninių tuščių įrašų.")
            st.exception(exc)
            st.stop()

    if manager_start_date > manager_end_date:
        st.error("Data „nuo“ negali būti vėlesnė už datą „iki“.")
        st.stop()

    st.markdown("### DB tvarkymas")
    c_db1, c_db2 = st.columns([1, 3])
    with c_db1:
        duplicate_cleanup_main_btn = st.button("🧽 Ištrinti dublikatus DB", use_container_width=True, key="manager_duplicate_cleanup_main_btn")
    with c_db2:
        st.caption("Mygtukas patikrina naujausius manager_transactions įrašus ir ištrina dubliuotus techninius / veidrodinius įrašus. Ataskaitoje dublikatai paslepiami ir be trynimo.")

    if duplicate_cleanup_main_btn:
        try:
            with st.spinner("Ieškomi ir trinami vadovų sandorių dublikatai DB..."):
                stats = delete_duplicate_manager_transactions(limit=int(repair_limit))
            if stats.get("errors", 0):
                st.warning(
                    "Dublikatų valymas baigtas su klaidomis: "
                    f"patikrinta {stats.get('checked', 0)}, "
                    f"rasta dublikatų {stats.get('duplicates_found', 0)}, "
                    f"ištrinta {stats.get('deleted', 0)}, "
                    f"klaidų {stats.get('errors', 0)}."
                )
                if stats.get("error_messages"):
                    st.code("\n".join(stats.get("error_messages", [])[:10]))
            else:
                st.success(
                    "Dublikatų valymas baigtas: "
                    f"patikrinta {stats.get('checked', 0)}, "
                    f"rasta dublikatų {stats.get('duplicates_found', 0)}, "
                    f"ištrinta {stats.get('deleted', 0)}."
                )
            st.rerun()
        except Exception as exc:
            st.error("Nepavyko ištrinti dublikatų.")
            st.exception(exc)
            st.stop()

    raw_df = load_manager_transactions_from_db(manager_start_date, manager_end_date)
    raw_count = len(raw_df) if raw_df is not None else 0
    df = prepare_manager_transactions_df(raw_df)
    hidden_count = max(raw_count - len(df), 0)
    if hidden_count:
        st.caption(f"Ataskaitoje paslėpta techninių / tuščių PDF nuskaitymo įrašų: {hidden_count}.")
    if df.empty:
        st.info("Pasirinktu laikotarpiu vadovų sandorių duomenų nėra.")
        st.stop()

    news_start_date = manager_start_date - timedelta(days=370)
    news_end_date = manager_end_date + timedelta(days=370)
    crib_news_df = load_crib_news_df(news_start_date, news_end_date)
    dpl_periods_df = prepare_dpl_periods_df(crib_news_df)
    df = add_dpl_check_to_transactions(df, dpl_periods_df)

    with st.expander("DPL diagnostika", expanded=False):
        st.write("CRIB naujienų eilučių sk.:", len(crib_news_df))
        st.write("Identifikuotų metinių / pusmečio ataskaitų sk.:", len(dpl_periods_df))
        st.write("CRIB naujienų stulpeliai:")
        st.write(list(crib_news_df.columns))
        if dpl_periods_df.empty:
            st.warning(
                "DPL ataskaitų nerasta. Patikrink, ar market_news lentelėje yra CRIB "
                "naujienų su kategorijomis „Metinė informacija“ ir „Tarpinė informacija“, "
                "taip pat ar antraštėse yra metinės arba pusmečio / 6 mėn. ataskaitos požymių."
            )
        else:
            st.dataframe(dpl_periods_df[["issuer", "dpl_report_type", "report_published_date", "dpl_start_date", "dpl_end_date", "category", "title", "crib_url"]], use_container_width=True, hide_index=True)

        # Papildoma diagnostika: matome tarpinę informaciją, kuri nebuvo priskirta 6 mėn. ataskaitoms
        # dažniausiai todėl, kad tai 3 mėn. arba 9 mėn. rezultatai.
        if crib_news_df is not None and not crib_news_df.empty:
            tmp_diag = crib_news_df.copy()
            tmp_diag["report_class"] = tmp_diag.apply(_classify_financial_report, axis=1)
            interim_unmatched = tmp_diag[
                tmp_diag["category"].fillna("").astype(str).apply(_is_interim_category)
                & tmp_diag["report_class"].eq("")
            ].copy()
            if not interim_unmatched.empty:
                st.markdown("**Tarpinė informacija, neįtraukta į DPL kaip 6 mėn. ataskaita**")
                st.dataframe(
                    interim_unmatched[["issuer", "published_at", "category", "title", "crib_url"]],
                    use_container_width=True,
                    hide_index=True,
                )

    _show_summary_cards(df)
    st.markdown("---")

    with st.expander("Filtrai", expanded=True):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            df = _apply_multiselect_filter(df, "issuer", "Emitentas")
        with c2:
            df = _apply_multiselect_filter(df, "person_name", "Vadovas / susijęs asmuo")
        with c3:
            df = _apply_multiselect_filter(df, "transaction_type", "Sandorio pobūdis")
        with c4:
            df = _apply_multiselect_filter(df, "parse_status", "Apdorojimo statusas")
        delay_filter = st.selectbox("Vėlavimo filtras", ["Visi", "Tik vėluojantys >3 d.", "Tik nevėluojantys <=3 d.", "Be apskaičiuoto termino"], key="mgr_delay_filter")
        if delay_filter == "Tik vėluojantys >3 d.":
            df = df[df["is_late_notification"] == True]
        elif delay_filter == "Tik nevėluojantys <=3 d.":
            df = df[(df["days_to_publish"].notna()) & (df["days_to_publish"] <= 3)]
        elif delay_filter == "Be apskaičiuoto termino":
            df = df[df["days_to_publish"].isna()]
        dpl_filter = st.selectbox("DPL filtras", ["Visi", "Tik sandoriai per DPL", "Tik ne DPL"], key="mgr_dpl_filter")
        if dpl_filter == "Tik sandoriai per DPL":
            df = df[df["is_dpl_period"] == True]
        elif dpl_filter == "Tik ne DPL":
            df = df[df["is_dpl_period"] == False]

    _show_tables(df)

    st.markdown("---")
    show_dpl_dates_table(dpl_periods_df)

    export_df = st.session_state.get("manager_transactions_display_df")
    if export_df is None or export_df.empty:
        export_df = _prepare_manager_transactions_display_df(df)

    st.download_button(
        "⬇ Atsisiųsti CSV",
        data=export_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="vadovu_sandoriai_su_dpl.csv",
        mime="text/csv",
        use_container_width=True,
    )

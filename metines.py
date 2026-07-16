"""
Metinių ataskaitų modulis Rinkos pulsui.

Šio failo paskirtis:
- paimti CRIB kategorijos „Metinė informacija“ / „Annual information“ pranešimus iš Supabase market_news;
- naudoti tik tuos emitentus, kurie yra Supabase market_issuers sąraše su market='VLN'
  ir, jei užpildytas segmentas, priklauso oficialiajam arba papildomajam sąrašui;
- atsisiųsti CRIB priedus per viešas file_url / viewAttachment nuorodas;
- iš PDF / ZIP / XHTML / XML / XBRL ištraukti tekstą ir pagrindinius finansinius rodiklius;
- įrašyti dokumentų informaciją į annual_reports ir annual_report_files;
- įrašyti rodiklius į normalizuotą annual_report_metrics lentelę:
      metric_name  = Turtas / Nuosavas kapitalas / Grynasis pelnas / Pajamos
      metric_group = Grupė / Bendrovė / Neatskirta
      metric_value = skaitinė reikšmė, tūkst. EUR
- parodyti rezultatų lentelę Streamlit puslapyje.

Svarbu: Storage nebūtinas. CRIB priedai yra vieši, todėl DB saugomas file_url,
raw_text ir ištraukti rodikliai. Tai nemokamas ir patikimas variantas, leidžiantis
ateityje iš tų pačių raw_text/file_url ištraukti papildomus rodiklius.
"""

import hashlib
import os
import re
import zipfile
import warnings
from collections import defaultdict
from datetime import date, timedelta
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote, urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
import pdfplumber
import urllib3
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=UserWarning)

try:
    from supabase_cache import load_news_df
except Exception:
    load_news_df = None


# ============================================================
# KONFIGŪRACIJA
# ============================================================

MODULE_VERSION = "metines_zip_esef_units_2026-07-16b"

TABLE_REPORTS = "annual_reports"
TABLE_FILES = "annual_report_files"
TABLE_METRICS = "annual_report_metrics"
TABLE_MARKET_NEWS = "market_news"
TABLE_MARKET_ISSUERS = "market_issuers"

ANNUAL_CATEGORY_TOKENS = (
    "metinė informacija",
    "metine informacija",
    "annual information",
)

# Tikros metinės ataskaitos. Filtras tyčia nėra per siauras, nes dalis emitentų
# naudoja formuluotes „audituoti rezultatai“, „metinių finansinių ataskaitų rinkinys“ ir pan.
REAL_ANNUAL_TITLE_TOKENS = (
    "metin",
    "annual",
    "audituot",
    "audited",
    "finansin",
    "financial",
    "ataskait",
    "report",
)

# Pranešimai, kurie gali būti CRIB kategorijoje „Metinė informacija“, bet paprastai
# nėra pats finansinių ataskaitų rinkinys.
SOFT_NON_REPORT_TITLE_TOKENS = (
    "skelbimo dat",
    "calendar",
    "kalendorius",
    "webinar",
    "pristatys",
    "presentation",
    "pristatymo medžiaga",
    "susirinkimo sušaukimo",
    "akcininkų susirinkimo",
)

OFFICIAL_SECONDARY_TOKENS = (
    "oficial", "official", "main list", "baltic main", "pagrind", "papild", "secondary", "additional",
)

EXCLUDED_SEGMENT_TOKENS = (
    "first north", "bond", "oblig", "fund", "etf", "vyriausyb", "government",
)

ALLOWED_ATTACHMENT_EXTENSIONS = (
    ".pdf", ".zip", ".xhtml", ".html", ".htm", ".xml", ".xbrl",
)

METRIC_ORDER = ["Turtas", "Nuosavas kapitalas", "Grynasis pelnas", "Pajamos"]
GROUP_ORDER = ["Grupė", "Bendrovė", "Neatskirta"]


# ============================================================
# BENDROS PAGALBINĖS FUNKCIJOS
# ============================================================


def _collapse_ws(value: Any) -> str:
    s = str(value or "")
    s = s.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    s = s.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _strip_accents_lithuanian(value: Any) -> str:
    s = str(value or "")
    repl = str.maketrans({
        "ą": "a", "č": "c", "ę": "e", "ė": "e", "į": "i", "š": "s", "ų": "u", "ū": "u", "ž": "z",
        "Ą": "A", "Č": "C", "Ę": "E", "Ė": "E", "Į": "I", "Š": "S", "Ų": "U", "Ū": "U", "Ž": "Z",
    })
    return s.translate(repl)


def _norm(value: Any) -> str:
    s = _strip_accents_lithuanian(value).lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _issuer_key(value: Any) -> str:
    s = _norm(value)
    # Teisinės formos šalinamos tik kaip atskiri žodžiai.
    s = re.sub(r"\b(akcine bendrove|uzdaroji akcine bendrove|ab|uab|as|asa|ou|sia)\b", " ", s)
    s = re.sub(r"\b(group|grupe|grupė)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _to_iso(value: Any) -> Optional[str]:
    try:
        if value is not None and not pd.isna(value):
            return pd.to_datetime(value, errors="coerce").isoformat()
    except Exception:
        pass
    return None


def _date_to_iso_date(value: Any) -> Optional[str]:
    try:
        dt = pd.to_datetime(value, errors="coerce")
        if pd.notna(dt):
            return dt.date().isoformat()
    except Exception:
        pass
    return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or pd.isna(value):
            return None
        return int(value)
    except Exception:
        return None


def _parse_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null", "-", "–", "—"}:
        return None

    # Neigiamos reikšmės dažnai pateikiamos skliaustuose.
    negative_by_parentheses = bool(re.search(r"\([^)]*\d[^)]*\)", s))

    s = s.replace("\u00a0", " ")
    s = s.replace("EUR", " ").replace("Eur", " ").replace("eur", " ")
    s = s.replace("€", " ")
    s = s.replace("'", "")

    # Paimame pirmą skaitinį fragmentą. Skaičiai gali būti „1 234“, „1.234“, „1,234.56“, „1 234,56“.
    m = re.search(r"[-+]?\d[\d\s.,]*", s)
    if not m:
        return None

    num = m.group(0).strip().replace(" ", "")
    if not num:
        return None

    # Jei yra ir taškas, ir kablelis, paskutinis separatorius laikomas dešimtainiu.
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        # Jei kablelis turi 3 skaitmenis po jo ir prieš jį yra bent 1 skaitmuo, dažnai tai tūkstančių skirtukas.
        if re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+", num):
            num = num.replace(",", "")
        else:
            num = num.replace(",", ".")
    elif "." in num:
        if re.fullmatch(r"[-+]?\d{1,3}(\.\d{3})+", num):
            num = num.replace(".", "")

    try:
        out = float(num)
        if negative_by_parentheses and out > 0:
            out = -out
        return out
    except Exception:
        return None


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content or b"").hexdigest()


def _file_name_from_url(url: str, fallback: str = "ataskaita") -> str:
    try:
        path = urlparse(url).path
        name = unquote(os.path.basename(path))
        if name and "." in name:
            return name[:240]
    except Exception:
        pass
    return fallback[:240]


def _content_type_guess(file_name: str, file_type: str) -> str:
    name = (file_name or "").lower()
    if file_type == "pdf" or name.endswith(".pdf"):
        return "application/pdf"
    if file_type == "zip" or name.endswith(".zip"):
        return "application/zip"
    if file_type in {"xhtml", "html"} or name.endswith((".xhtml", ".html", ".htm")):
        return "text/html"
    if file_type in {"xml", "xbrl"} or name.endswith((".xml", ".xbrl")):
        return "application/xml"
    return "application/octet-stream"


def _supabase_client_parts():
    from supabase_cache import _supabase_headers, _supabase_rest_url, _http_client
    return _supabase_headers, _supabase_rest_url, _http_client


def _response_is_missing_table(resp) -> bool:
    try:
        if int(getattr(resp, "status_code", 0) or 0) == 404:
            return True
        txt = str(getattr(resp, "text", "") or "").lower()
        return "could not find the table" in txt or "schema cache" in txt or "404 not found" in txt
    except Exception:
        return False


def _rest_select(table: str, params: Dict[str, Any]) -> List[dict]:
    headers, rest_url, http_client = _supabase_client_parts()
    url = rest_url(table)
    with http_client() as client:
        resp = client.get(url, headers=headers(), params=params)
        if _response_is_missing_table(resp):
            raise RuntimeError(f"Supabase lentelė `{table}` nerasta arba nematoma REST schema cache.")
        resp.raise_for_status()
        return resp.json() or []


def _rest_insert(table: str, row: Dict[str, Any], return_representation: bool = True) -> List[dict]:
    headers, rest_url, http_client = _supabase_client_parts()
    url = rest_url(table)
    prefer = "return=representation" if return_representation else "return=minimal"
    with http_client() as client:
        resp = client.post(url, headers={**headers(), "Prefer": prefer}, json=row)
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"Supabase INSERT klaida `{table}`: {resp.status_code} - {resp.text}")
        if resp.status_code == 204:
            return []
        try:
            return resp.json() or []
        except Exception:
            return []


def _rest_patch(table: str, filters: Dict[str, str], row: Dict[str, Any], return_representation: bool = True) -> List[dict]:
    headers, rest_url, http_client = _supabase_client_parts()
    url = rest_url(table)
    prefer = "return=representation" if return_representation else "return=minimal"
    with http_client() as client:
        resp = client.patch(url, headers={**headers(), "Prefer": prefer}, params=filters, json=row)
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"Supabase PATCH klaida `{table}`: {resp.status_code} - {resp.text}")
        if resp.status_code == 204:
            return []
        try:
            return resp.json() or []
        except Exception:
            return []


def _rest_delete(table: str, filters: Dict[str, str]) -> int:
    headers, rest_url, http_client = _supabase_client_parts()
    url = rest_url(table)
    with http_client() as client:
        resp = client.delete(url, headers={**headers(), "Prefer": "return=representation"}, params=filters)
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"Supabase DELETE klaida `{table}`: {resp.status_code} - {resp.text}")
        try:
            return len(resp.json() or [])
        except Exception:
            return 0


# ============================================================
# EMITENTAI: TIK VLN OFICIALUSIS / PAPILDOMASIS
# ============================================================


def load_vln_official_secondary_issuers() -> pd.DataFrame:
    """Užkrauna leidžiamus emitentus iš market_issuers.

    Jei `segment` stulpelyje yra reikšmių, paliekami tik oficialusis / papildomasis sąrašas.
    Jei `segment` tuščias visiems, paliekami visi market='VLN' įrašai, nes kai kuriuose projektuose
    `market_issuers` jau būna iš anksto užpildyta tik reikiamais emitentais.
    """
    try:
        rows = _rest_select(
            TABLE_MARKET_ISSUERS,
            {
                "select": "*",
                "market": "eq.VLN",
                "order": "issuer.asc",
                "limit": "5000",
            },
        )
    except Exception as exc:
        st.warning(f"Nepavyko užkrauti market_issuers: {exc}")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    if "issuer" not in df.columns:
        df["issuer"] = df["company"] if "company" in df.columns else ""

    for c in ["issuer", "company", "ticker", "segment", "issuer_norm", "company_norm"]:
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("").astype(str).map(_collapse_ws)

    # Aktyvumo filtrai, jei tokie egzistuoja.
    for active_col in ["is_active", "active", "listed"]:
        if active_col in df.columns:
            mask = df[active_col].fillna(True).astype(str).str.lower().isin(["true", "1", "yes", "taip"])
            if mask.any():
                df = df[mask].copy()
            break

    # Segmento filtras.
    if "segment" in df.columns and df["segment"].fillna("").astype(str).str.strip().ne("").any():
        seg = df["segment"].fillna("").astype(str).str.lower()
        keep = seg.apply(lambda x: any(t in x for t in OFFICIAL_SECONDARY_TOKENS))
        excluded = seg.apply(lambda x: any(t in x for t in EXCLUDED_SEGMENT_TOKENS))
        df = df[keep & ~excluded].copy()

    df["issuer"] = df["issuer"].where(df["issuer"].ne(""), df["company"])
    df = df[df["issuer"].fillna("").astype(str).str.strip().ne("")].copy()

    db_norm = df["issuer_norm"].where(df["issuer_norm"].ne(""), df["company_norm"])
    df["issuer_key"] = db_norm.where(db_norm.fillna("").astype(str).str.strip().ne(""), df["issuer"].map(_issuer_key))
    df["issuer_key"] = df["issuer_key"].map(_issuer_key)
    df = df[df["issuer_key"].ne("")].copy()

    keep_cols = [c for c in ["issuer", "company", "ticker", "segment", "issuer_norm", "company_norm", "issuer_key"] if c in df.columns]
    return df[keep_cols].drop_duplicates(subset=["issuer_key"]).reset_index(drop=True)


def _canonical_issuer_from_allowed(value: Any, allowed: pd.DataFrame) -> str:
    if allowed is None or allowed.empty:
        return _collapse_ws(value)
    key = _issuer_key(value)
    if not key:
        return ""
    lookup = dict(zip(allowed["issuer_key"], allowed["issuer"]))
    if key in lookup:
        return str(lookup[key])
    for k, issuer in lookup.items():
        if key and k and (key in k or k in key):
            return str(issuer)
    return ""


def _infer_issuer_from_text(title: Any, content: Any, allowed: pd.DataFrame) -> str:
    if allowed is None or allowed.empty:
        return ""
    text_key = _issuer_key(f"{title or ''} {content or ''}")
    best = ""
    best_len = 0
    for _, row in allowed.iterrows():
        key = str(row.get("issuer_key") or "")
        issuer = str(row.get("issuer") or "")
        if len(key) < 3:
            continue
        if re.search(rf"(?:^|\s){re.escape(key)}(?:\s|$)", text_key):
            if len(key) > best_len:
                best = issuer
                best_len = len(key)
    return best


# ============================================================
# MARKET_NEWS: PUSLAPIAVIMAS IR METINIŲ PRANEŠIMŲ ATRANKA
# ============================================================


def _load_market_news_paginated(source: str, start_date: date, end_date: date, page_size: int = 1000) -> pd.DataFrame:
    rows_all: List[dict] = []
    offset = 0
    start_iso = f"{start_date}T00:00:00"
    end_iso = f"{end_date}T23:59:59"

    while True:
        params = {
            "select": "*",
            "source": f"eq.{source}",
            "published_at": [f"gte.{start_iso}", f"lte.{end_iso}"],
            "order": "published_at.asc",
            "limit": str(page_size),
            "offset": str(offset),
        }
        batch = _rest_select(TABLE_MARKET_NEWS, params)
        if not batch:
            break
        rows_all.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
        if offset > 100000:
            break

    return pd.DataFrame(rows_all)


def _load_crib_news_df(start_date: date, end_date: date) -> pd.DataFrame:
    """Krauna CRIB naujienas. Pirma bandoma tiesiogiai su puslapiavimu, tada fallback į load_news_df."""
    try:
        df = _load_market_news_paginated("crib", start_date, end_date, page_size=1000)
        if df is not None:
            return df
    except Exception:
        pass

    if load_news_df is None:
        return pd.DataFrame()
    try:
        return load_news_df("crib", start_date, end_date)
    except Exception:
        return pd.DataFrame()


def _is_annual_information_row(row: pd.Series) -> bool:
    text = " ".join([
        str(row.get("category", "") or ""),
        str(row.get("title", "") or ""),
        str(row.get("content", "") or "")[:1000],
    ]).lower()
    return any(t in text for t in ANNUAL_CATEGORY_TOKENS)


def _looks_like_real_annual_report(row: pd.Series) -> bool:
    title = str(row.get("title", "") or "").lower()
    content = str(row.get("content", "") or "").lower()
    text = f"{title} {content[:1500]}"

    if not any(t in text for t in REAL_ANNUAL_TITLE_TOKENS):
        return False

    # Jeigu tai tik datos ar kalendoriaus pranešimas ir nėra priedų požymio, neparsinsime kaip ataskaitos.
    if any(t in title for t in SOFT_NON_REPORT_TITLE_TOKENS):
        if "pried" not in content and "attachment" not in content and "viewattachment" not in content:
            return False

    return True


def _extract_report_year_from_text(*values: Any) -> Optional[int]:
    text = " ".join(str(v or "") for v in values)
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text)]
    if not years:
        return None
    # Ataskaitos pavadinime dažnai yra paskelbimo metai ir ataskaitiniai metai.
    # Dažniausiai reikalingas mažiausias / ankstesnis tarp 2024, 2025, kai pavadinime „už 2024 m.“.
    # Jei yra keli, imam dažniausiai minimą, o jei lygu - mažiausią.
    counts = defaultdict(int)
    for y in years:
        counts[y] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def load_annual_crib_news(start_date: date, end_date: date, allowed: pd.DataFrame) -> pd.DataFrame:
    raw = _load_crib_news_df(start_date, end_date)
    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.copy()
    for col in ["company", "category", "title", "published_at", "url", "content"]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str).map(_collapse_ws)

    df = df[df.apply(_is_annual_information_row, axis=1)].copy()
    if df.empty:
        return df

    df = df[df.apply(_looks_like_real_annual_report, axis=1)].copy()
    if df.empty:
        return df

    df["issuer"] = df["company"].apply(lambda x: _canonical_issuer_from_allowed(x, allowed))
    missing = df["issuer"].fillna("").astype(str).str.strip().eq("")
    if missing.any():
        df.loc[missing, "issuer"] = df.loc[missing].apply(
            lambda r: _infer_issuer_from_text(r.get("title", ""), r.get("content", ""), allowed),
            axis=1,
        )

    df = df[df["issuer"].fillna("").astype(str).str.strip().ne("")].copy()
    if df.empty:
        return df

    df["issuer_norm"] = df["issuer"].map(_issuer_key)
    df["crib_url"] = df["url"].fillna("").astype(str).str.strip()
    df["published_at_dt"] = pd.to_datetime(df["published_at"], errors="coerce")
    df["report_year"] = df.apply(lambda r: _extract_report_year_from_text(r.get("title"), r.get("content"), r.get("published_at")), axis=1)
    df = df.sort_values("published_at_dt", ascending=False)
    df = df.drop_duplicates(subset=["crib_url"], keep="first")
    return df.reset_index(drop=True)


# ============================================================
# CRIB PRIEDŲ NUORODOS
# ============================================================


def _rank_attachment_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    clean: List[Dict[str, str]] = []
    for item in items:
        u = (item.get("url") or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        clean.append(item)

    def score(item: Dict[str, str]) -> int:
        u = (item.get("url") or "").lower()
        name = (item.get("name") or "").lower()
        txt = f"{u} {name}"
        if ".zip" in txt:
            return 0
        if ".xhtml" in txt or ".xbrl" in txt or ".xml" in txt or "esef" in txt:
            return 1
        if ".pdf" in txt and ("financial" in txt or "finansin" in txt or "fa" in txt or "conso" in txt or "ifrs" in txt):
            return 2
        if ".pdf" in txt:
            return 3
        if "viewattachment" in txt:
            return 4
        if "globenewswire" in txt:
            return 8
        return 5

    return sorted(clean, key=score)


def _extract_attachment_items_from_html(html: str, base_url: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    items: List[Dict[str, str]] = []

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = _collapse_ws(a.get_text(" ", strip=True))
        href_l = href.lower()
        text_l = text.lower()
        if (
            any(ext in href_l for ext in ALLOWED_ATTACHMENT_EXTENSIONS)
            or "viewattachment.action" in href_l
            or "download" in href_l
            or "attachment" in href_l
            or any(ext.replace(".", "") in text_l for ext in ALLOWED_ATTACHMENT_EXTENSIONS)
            or "xbrl" in text_l
            or "esef" in text_l
        ):
            items.append({"url": urljoin(base_url, href), "name": text or _file_name_from_url(href)})

    # Kartais market_news content turi plain text tipo „Priedai: file.pdf /cns-web/oam/viewAttachment...“.
    # Ištraukiame tokius linkus iš teksto.
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    for m in re.finditer(r"((?:/cns-web/oam/viewAttachment\.action\?messageAttachmentId=\d+)|(?:https?://[^\s'\"]+))", text):
        raw = m.group(1)
        if "viewAttachment" in raw or any(ext in raw.lower() for ext in ALLOWED_ATTACHMENT_EXTENSIONS):
            items.append({"url": urljoin(base_url, raw), "name": _file_name_from_url(raw)})

    return _rank_attachment_items(items)


def get_crib_attachment_items(crib_url: str, content_hint: str = "") -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    if crib_url:
        try:
            resp = requests.get(crib_url, headers=headers, verify=False, timeout=35)
            resp.raise_for_status()
            items.extend(_extract_attachment_items_from_html(resp.text, crib_url))
        except Exception:
            pass

    if content_hint:
        pseudo_html = f"<html><body>{content_hint}</body></html>"
        items.extend(_extract_attachment_items_from_html(pseudo_html, crib_url or "https://www.crib.lt/"))

    return _rank_attachment_items(items)


def _detect_file_type(content: bytes, url: str = "", name: str = "") -> str:
    sample = (content or b"")[:1000].lstrip()
    txt = sample[:500].lower()
    joined = f"{url} {name}".lower()
    if sample.startswith(b"%PDF") or ".pdf" in joined:
        return "pdf"
    if sample.startswith(b"PK\x03\x04") or ".zip" in joined:
        return "zip"
    if b"<html" in txt or b"<xhtml" in txt or b"ix:" in txt or ".xhtml" in joined or ".html" in joined or ".htm" in joined:
        return "xhtml"
    if b"<?xml" in txt or b"<xbrl" in txt or b"xbrl" in txt or ".xml" in joined or ".xbrl" in joined:
        return "xml"
    return "unknown"


def download_attachment(url: str, name: str = "") -> Tuple[bytes, str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/pdf,application/xhtml+xml,application/xml,text/html,application/zip,*/*",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(url, headers=headers, verify=False, timeout=90, allow_redirects=True)
    resp.raise_for_status()
    content = resp.content or b""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    detected = _detect_file_type(content, url=url, name=name)
    if detected == "unknown":
        if "pdf" in ctype:
            detected = "pdf"
        elif "zip" in ctype:
            detected = "zip"
        elif "xml" in ctype or "html" in ctype or "xbrl" in ctype:
            detected = "xml"
    return content, detected, ctype


# ============================================================
# PDF / TEKSTO / XBRL PARSINIMAS
# ============================================================


METRIC_LABELS = {
    "Turtas": [
        "turtas iš viso", "turtas is viso", "viso turto", "visas turtas", "iš viso turto", "is viso turto", "total assets", "assets total",
    ],
    "Nuosavas kapitalas": [
        "nuosavas kapitalas iš viso", "nuosavas kapitalas is viso", "iš viso nuosavo kapitalo", "viso nuosavo kapitalo",
        "nuosavas kapitalas", "equity", "total equity",
        "equity total", "total shareholders equity", "shareholders equity", "equity attributable",
    ],
    "Grynasis pelnas": [
        "grynasis pelnas", "grynasis nuostolis", "grynasis pelnas (nuostoliai)", "grynasis pelnas nuostoliai",
        "metų pelnas", "metu pelnas", "metų nuostolis", "metu nuostolis", "laikotarpio pelnas", "laikotarpio nuostolis",
        "profit loss", "profit (loss)", "net profit", "net loss", "profit for the year", "loss for the year",
    ],
    "Pajamos": [
        "pardavimo pajamos", "pagrindinės veiklos pajamos", "pagrindines veiklos pajamos", "pajamos",
        "revenue", "sales revenue", "net sales", "sales",
    ],
}

NEGATIVE_LABEL_GUARDS = {
    "Pajamos": ["finansinės pajamos", "finansines pajamos", "kitos pajamos", "palūkanų pajamos", "palukanu pajamos", "other income", "finance income"],
    "Turtas": ["ilgalaikis turtas", "trumpalaikis turtas", "finansinis turtas", "investicinis turtas", "non current assets", "current assets"],
    "Nuosavas kapitalas": ["nuosavas kapitalas ir įsipareigojimai", "nuosavas kapitalas ir isipareigojimai", "equity and liabilities", "total equity and liabilities"],
}

CONCEPTS = {
    "Turtas": {
        "assets", "ifrsfullassets", "ifrsfull_assets", "ifrs_full_assets",
    },
    "Nuosavas kapitalas": {
        "equity", "ifrsfullequity", "ifrsfull_equity", "ifrs_full_equity",
        "equityattributabletoownersofparent", "ifrsfullequityattributabletoownersofparent",
    },
    "Grynasis pelnas": {
        "profitloss", "profitlossattributabletoownersofparent", "profitlossfromcontinuingoperations",
        "ifrsfullprofitloss", "ifrsfull_profitloss",
    },
    "Pajamos": {
        "revenue", "salesrevenue", "ifrsfullrevenue", "ifrs_full_revenue", "ifrsfull_revenue",
        "revenuefromcontractswithcustomers", "revenuefromcontractswithcustomersexcludingassessedtax",
        "ifrsfullrevenuefromcontractswithcustomers", "ifrsfullrevenuefromcontractswithcustomersexcludingassessedtax",
    },
}


def _label_matches(metric_name: str, row_norm: str) -> bool:
    if not row_norm:
        return False
    for bad in NEGATIVE_LABEL_GUARDS.get(metric_name, []):
        if _norm(bad) in row_norm:
            # Special case: "nuosavas kapitalas ir įsipareigojimai" is not equity itself.
            return False
    return any(_norm(label) in row_norm for label in METRIC_LABELS[metric_name])


def _extract_pdf_text_and_tables(content: bytes) -> Tuple[str, List[List[List[Any]]]]:
    text_parts: List[str] = []
    tables: List[List[List[Any]]] = []

    with pdfplumber.open(BytesIO(content)) as pdf:
        for page in pdf.pages:
            try:
                txt = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            except Exception:
                try:
                    txt = page.extract_text() or ""
                except Exception:
                    txt = ""
            if txt:
                text_parts.append(txt)

            # Keli table extraction režimai, nes finansinių ataskaitų lentelės skiriasi.
            for settings in (
                None,
                {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
                {"vertical_strategy": "text", "horizontal_strategy": "text"},
            ):
                try:
                    extracted = page.extract_tables(table_settings=settings) if settings else page.extract_tables()
                    for tbl in extracted or []:
                        if tbl and len(tbl) >= 1:
                            tables.append(tbl)
                except Exception:
                    continue

    return "\n".join(text_parts).strip(), tables


def _numbers_from_cells(cells: Iterable[Any]) -> List[float]:
    nums: List[float] = []
    for cell in cells:
        s = _collapse_ws(cell)
        if not s:
            continue
        # Dažnas note stulpelis: vienaženklis / dviženklis pastabos numeris.
        # Jo neišmetame čia, o pašaliname vėliau pagal poziciją.
        parts = re.findall(r"\(?[-+]?\d[\d\s.,]*\)?", s)
        if not parts:
            continue
        # Jei langelyje yra ilgas tekstas su daug skaičių, dažnai tai ne finansinė reikšmė.
        # Lentelių eilutėse paprastai vienas langelis = viena reikšmė.
        if len(parts) > 2 and len(s) > 40:
            continue
        for p in parts[:2]:
            v = _parse_number(p)
            if v is not None:
                nums.append(v)
    return nums


def _clean_statement_numbers(nums: List[float]) -> List[float]:
    if not nums:
        return []
    out = list(nums)
    # Pašaliname pastabų numerius eilutės pradžioje, kai po jų yra didesnės reikšmės.
    while len(out) >= 3 and abs(out[0]) < 100 and any(abs(x) >= 100 for x in out[1:]):
        out = out[1:]
    # Jei liko daugiau nei 4, imame pirmus 4 po note numerių pašalinimo.
    if len(out) > 4:
        out = out[:4]
    return out


def _infer_unit_kind(text: str) -> Tuple[str, str]:
    """Nustato, kokiais vienetais pateikiami skaičiai.

    Grąžina (unit_kind, note):
    - thousands_eur: reikšmės jau yra tūkst. EUR;
    - eur: reikšmės yra EUR ir jas reikia dalinti iš 1000;
    - unknown: sprendžiama pagal reikšmės dydį.
    """
    n = _norm(text)
    if not n:
        return "unknown", "vienetai nenustatyti"

    thousand_patterns = [
        r"tukst\s*eur", r"tukstanciais\s*eur", r"tukstanciu\s*eur",
        r"thousand\s*eur", r"eur\s*000", r"000\s*eur", r"thousands\s*of\s*eur",
        r"keur", r"k\s*eur",
    ]
    if any(re.search(p, n) for p in thousand_patterns):
        return "thousands_eur", "originalūs skaičiai pateikti tūkst. EUR"

    eur_patterns = [
        r"eurais", r"euru", r"eur ", r" euro", r" euros", r"currency eur", r"unit eur",
        r"expressed in euros", r"presented in euros",
    ]
    if any(re.search(p, n + " ") for p in eur_patterns):
        return "eur", "originalūs skaičiai pateikti EUR; DB saugoma tūkst. EUR"

    return "unknown", "vienetai nenustatyti; taikoma reikšmės dydžio taisyklė"


def _value_to_teu(value: Optional[float], unit_kind: str = "unknown") -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
    except Exception:
        return None
    if unit_kind == "eur":
        return v / 1000.0
    if unit_kind == "thousands_eur":
        return v
    # Kai vienetas neaiškus, Lietuvos listinguotų bendrovių metinių ataskaitų
    # EUR reikšmės dažnai yra milijoninės. Tokias konvertuojame į tūkst. EUR.
    if abs(v) >= 1_000_000:
        return v / 1000.0
    return v


def _scope_from_text(text: str) -> str:
    n = _norm(text)
    has_group = any(t in n for t in ["grupe", "konsolid", "consolidated", "group"])
    has_company = any(t in n for t in ["bendrove", "imone", "atskiros", "separate", "company", "parent"])
    if has_group and has_company:
        return "group_company"
    if has_group:
        return "group_only"
    if has_company:
        return "company_only"
    return "unknown"


def _table_scope(table: List[List[Any]], row_idx: int) -> str:
    header_rows = table[max(0, row_idx - 6):row_idx]
    header_text = " ".join(" ".join(_collapse_ws(c) for c in row if _collapse_ws(c)) for row in header_rows)
    return _scope_from_text(header_text)


def _assign_group_company(nums: List[float], scope: str = "unknown", unit_kind: str = "unknown") -> Dict[str, Optional[float]]:
    nums = _clean_statement_numbers(nums)
    if not nums:
        return {"Grupė": None, "Bendrovė": None, "Neatskirta": None}

    def conv(x):
        return _value_to_teu(x, unit_kind)

    if scope == "group_company":
        if len(nums) >= 4:
            # Dažniausia forma: Grupė einami metai, Grupė ankstesni metai, Bendrovė einami metai, Bendrovė ankstesni metai.
            return {"Grupė": conv(nums[0]), "Bendrovė": conv(nums[2]), "Neatskirta": None}
        if len(nums) >= 2:
            return {"Grupė": conv(nums[0]), "Bendrovė": conv(nums[1]), "Neatskirta": None}

    if scope == "group_only":
        return {"Grupė": conv(nums[0]), "Bendrovė": None, "Neatskirta": None}

    if scope == "company_only":
        return {"Grupė": None, "Bendrovė": conv(nums[0]), "Neatskirta": None}

    # Jeigu scope nežinomas, nesupainiojame ankstesnių metų su bendrovės reikšme.
    # 4 reikšmės dažnai vis tiek reiškia Grupė/Bendrovė per 2 metus, todėl galime atskirti.
    if len(nums) >= 4:
        return {"Grupė": conv(nums[0]), "Bendrovė": conv(nums[2]), "Neatskirta": None}
    return {"Grupė": None, "Bendrovė": None, "Neatskirta": conv(nums[0])}


def _extract_metrics_from_tables(tables: List[List[List[Any]]], document_text: str = "") -> Dict[Tuple[str, str], Dict[str, Any]]:
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}
    doc_unit_kind, doc_unit_note = _infer_unit_kind(document_text)

    for table_idx, table in enumerate(tables or []):
        table_text = " ".join(
            " ".join(_collapse_ws(c) for c in row if _collapse_ws(c))
            for row in (table or [])[:12]
        )
        table_unit_kind, table_unit_note = _infer_unit_kind(table_text + " " + document_text[:3000])
        unit_kind = table_unit_kind if table_unit_kind != "unknown" else doc_unit_kind
        unit_note = table_unit_note if table_unit_kind != "unknown" else doc_unit_note

        for row_idx, row in enumerate(table or []):
            if not row:
                continue
            row_text = " ".join(_collapse_ws(c) for c in row if _collapse_ws(c))
            row_norm = _norm(row_text)
            if not row_norm:
                continue

            scope = _table_scope(table, row_idx)
            if scope == "unknown":
                scope = _scope_from_text(table_text)

            for metric_name in METRIC_ORDER:
                if not _label_matches(metric_name, row_norm):
                    continue
                nums = _numbers_from_cells(row)
                assigned = _assign_group_company(nums, scope=scope, unit_kind=unit_kind)
                for group, value in assigned.items():
                    if value is None:
                        continue
                    key = (metric_name, group)
                    if key not in found:
                        found[key] = {
                            "value": value,
                            "source": "pdf_table",
                            "note": f"PDF lentelė {table_idx + 1}, eilutė {row_idx + 1}: {row_text[:220]}; {unit_note}; scope={scope}",
                            "confidence": 95 if group in {"Grupė", "Bendrovė"} and scope != "unknown" else 72,
                            "unit_note": unit_note,
                        }
    return found


def _extract_metrics_from_text_lines(text: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}
    unit_kind, unit_note = _infer_unit_kind(text[:8000])
    lines = [line for line in (text or "").splitlines() if _collapse_ws(line)]

    for line_idx, line in enumerate(lines):
        line_clean = _collapse_ws(line)
        line_norm = _norm(line_clean)
        if not line_norm:
            continue
        scope = _scope_from_text(" ".join(lines[max(0, line_idx - 5):line_idx + 1]))
        for metric_name in METRIC_ORDER:
            if not _label_matches(metric_name, line_norm):
                continue
            nums = _numbers_from_cells([line_clean])
            assigned = _assign_group_company(nums, scope=scope, unit_kind=unit_kind)
            for group, value in assigned.items():
                if value is None:
                    continue
                key = (metric_name, group)
                if key not in found:
                    found[key] = {
                        "value": value,
                        "source": "text_line",
                        "note": f"Teksto eilutė {line_idx + 1}: {line_clean[:220]}; {unit_note}; scope={scope}",
                        "confidence": 65 if scope != "unknown" else 50,
                        "unit_note": unit_note,
                    }
    return found


def _concept_key(value: Any) -> str:
    s = str(value or "")
    if "}" in s:
        s = s.split("}", 1)[1]
    if ":" in s:
        s = s.split(":")[-1]
    return _norm(s).replace(" ", "")


def _parse_ixbrl_number(tag) -> Optional[float]:
    value = tag.get_text("", strip=True)
    val = _parse_number(value)
    if val is None:
        return None
    try:
        scale = tag.get("scale")
        if scale not in (None, ""):
            val = val * (10 ** int(scale))
    except Exception:
        pass
    try:
        sign = str(tag.get("sign") or "")
        if sign == "-" and val > 0:
            val = -val
    except Exception:
        pass
    return val


def _xbrl_value_to_teu(value: float) -> float:
    # XBRL faktai dažniausiai yra EUR vienetais. Saugome tūkst. EUR.
    # Jei reikšmė akivaizdžiai didelė, konvertuojame į tūkst. EUR.
    if value is None:
        return value
    if abs(value) >= 1_000_000:
        return value / 1000.0
    return value


def _extract_metrics_from_xbrl(content: bytes) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], str]:
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}
    raw_text = ""
    text = content.decode("utf-8", errors="ignore")
    raw_text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)[:120000]

    # Inline XBRL / XHTML: ix:nonFraction su name="ifrs-full:Assets".
    soup = BeautifulSoup(text, "html.parser")
    fact_tags = []
    for tag in soup.find_all(True):
        name_attr = tag.get("name") or tag.get("contextref") and tag.name
        if tag.get("name"):
            fact_tags.append(tag)

    for tag in fact_tags:
        concept = _concept_key(tag.get("name"))
        val = _parse_ixbrl_number(tag)
        if val is None:
            continue
        ctx = str(tag.get("contextref") or tag.get("contextRef") or "")
        group = "Neatskirta"
        if re.search(r"consolid|group|grupe|konsolid", ctx, re.I):
            group = "Grupė"
        elif re.search(r"separate|company|bendrov|parent|individual", ctx, re.I):
            group = "Bendrovė"

        for metric_name, concepts in CONCEPTS.items():
            if concept in concepts:
                key = (metric_name, group)
                value_teu = _xbrl_value_to_teu(val)
                unit_ref = str(tag.get("unitref") or tag.get("unitRef") or "")
                unit_note = "XBRL reikšmė konvertuota / saugoma tūkst. EUR"
                if key not in found:
                    found[key] = {
                        "value": value_teu,
                        "source": "xbrl_fact",
                        "note": f"XBRL concept={concept}, context={ctx}, unitRef={unit_ref}; {unit_note}",
                        "confidence": 88 if group != "Neatskirta" else 68,
                        "unit_note": unit_note,
                    }

    # Paprastas XML XBRL: tagas pats yra concept.
    try:
        root = ET.fromstring(content)
        for elem in root.iter():
            concept = _concept_key(elem.tag)
            val = _parse_number(elem.text)
            if val is None:
                continue
            ctx = elem.attrib.get("contextRef") or elem.attrib.get("contextref") or ""
            group = "Neatskirta"
            if re.search(r"consolid|group|grupe|konsolid", ctx, re.I):
                group = "Grupė"
            elif re.search(r"separate|company|bendrov|parent|individual", ctx, re.I):
                group = "Bendrovė"
            for metric_name, concepts in CONCEPTS.items():
                if concept in concepts:
                    key = (metric_name, group)
                    value_teu = _xbrl_value_to_teu(val)
                    unit_ref = elem.attrib.get("unitRef") or elem.attrib.get("unitref") or ""
                    unit_note = "XBRL reikšmė konvertuota / saugoma tūkst. EUR"
                    if key not in found:
                        found[key] = {
                            "value": value_teu,
                            "source": "xbrl_fact",
                            "note": f"XBRL concept={concept}, context={ctx}, unitRef={unit_ref}; {unit_note}",
                            "confidence": 88 if group != "Neatskirta" else 68,
                            "unit_note": unit_note,
                        }
    except Exception:
        pass

    # Jei XBRL faktai nerasti, bandome tekstines eilutes.
    if not found and raw_text:
        found.update(_extract_metrics_from_text_lines(raw_text))

    return found, raw_text


def parse_pdf_content(content: bytes) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], str, Dict[str, Any]]:
    text, tables = _extract_pdf_text_and_tables(content)
    metrics = _extract_metrics_from_tables(tables, document_text=text)
    fallback = _extract_metrics_from_text_lines(text)
    for key, val in fallback.items():
        if key not in metrics:
            metrics[key] = val
    diag = {"pdf_tables_found": len(tables), "raw_text_len": len(text or "")}
    return metrics, text[:120000], diag


def parse_any_content(content: bytes, file_type: str, file_name: str = "") -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], str, Dict[str, Any]]:
    diag: Dict[str, Any] = {"file_type": file_type, "file_name": file_name}
    if not content:
        return {}, "", {**diag, "error": "empty_content"}

    if file_type == "pdf":
        try:
            metrics, raw_text, d = parse_pdf_content(content)
            return metrics, raw_text, {**diag, **d, "parse_status": "parsed_pdf" if metrics else "parsed_pdf_no_metrics"}
        except Exception as exc:
            return {}, "", {**diag, "parse_status": "pdf_parse_error", "error": str(exc)[:500]}

    if file_type in {"xhtml", "html", "xml", "xbrl"}:
        try:
            metrics, raw_text = _extract_metrics_from_xbrl(content)
            return metrics, raw_text, {**diag, "parse_status": "parsed_xbrl" if metrics else "parsed_xbrl_no_metrics", "raw_text_len": len(raw_text)}
        except Exception as exc:
            return {}, "", {**diag, "parse_status": "xbrl_parse_error", "error": str(exc)[:500]}

    # Neatpažintas tekstinis failas.
    try:
        text = content.decode("utf-8", errors="ignore")
        raw_text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
        metrics = _extract_metrics_from_text_lines(raw_text)
        return metrics, raw_text[:120000], {**diag, "parse_status": "parsed_text" if metrics else "parsed_text_no_metrics", "raw_text_len": len(raw_text)}
    except Exception as exc:
        return {}, "", {**diag, "parse_status": "unsupported_or_parse_error", "error": str(exc)[:500]}


# ============================================================
# SUPABASE ĮRAŠYMAS: annual_reports / files / metrics
# ============================================================


def ensure_annual_report(news: pd.Series) -> Optional[int]:
    crib_url = _collapse_ws(news.get("crib_url") or news.get("url") or "")
    if not crib_url:
        return None

    report_year = _safe_int(news.get("report_year")) or _extract_report_year_from_text(news.get("title"), news.get("content"), news.get("published_at"))
    issuer = _collapse_ws(news.get("issuer"))
    issuer_norm = _issuer_key(issuer)

    row = {
        "issuer": issuer,
        "issuer_norm": issuer_norm,
        "company": issuer,
        "market": "VLN",
        "report_year": report_year,
        "report_type": "Metinė",
        "crib_url": crib_url,
        "crib_title": _collapse_ws(news.get("title")),
        "crib_category": _collapse_ws(news.get("category")),
        "published_at": _to_iso(news.get("published_at")),
        "parse_status": "report_found",
        "updated_at": _to_iso(pd.Timestamp.utcnow()),
    }

    existing = _rest_select(TABLE_REPORTS, {"select": "id", "crib_url": f"eq.{crib_url}", "limit": "1"})
    if existing:
        report_id = int(existing[0]["id"])
        patch = {k: v for k, v in row.items() if k != "crib_url"}
        _rest_patch(TABLE_REPORTS, {"id": f"eq.{report_id}"}, patch, return_representation=False)
        return report_id

    inserted = _rest_insert(TABLE_REPORTS, row, return_representation=True)
    if inserted:
        return int(inserted[0]["id"])

    existing = _rest_select(TABLE_REPORTS, {"select": "id", "crib_url": f"eq.{crib_url}", "limit": "1"})
    return int(existing[0]["id"]) if existing else None


def ensure_annual_report_file(
    annual_report_id: int,
    issuer: str,
    report_year: Optional[int],
    file_url: str,
    file_name: str,
    file_type: str,
    content: bytes,
    raw_text: str,
    parse_status: str,
) -> Optional[int]:
    if not annual_report_id or not file_url:
        return None

    row = {
        "annual_report_id": annual_report_id,
        "issuer": issuer,
        "report_year": report_year,
        "file_url": file_url,
        "file_name": file_name[:240] if file_name else _file_name_from_url(file_url),
        "file_type": file_type,
        "content_type": _content_type_guess(file_name, file_type),
        "file_size": len(content or b""),
        "raw_text": (raw_text or "")[:120000],
        "parse_status": parse_status,
    }

    existing = _rest_select(TABLE_FILES, {"select": "id,raw_text,parse_status", "file_url": f"eq.{file_url}", "limit": "1"})
    if existing:
        file_id = int(existing[0]["id"])
        old_raw = str(existing[0].get("raw_text") or "")
        patch = dict(row)
        patch.pop("file_url", None)
        # Jei senas raw_text ilgesnis ir naujas tuščias, nebloginame.
        if old_raw and len(old_raw) > len(row.get("raw_text") or ""):
            patch.pop("raw_text", None)
        _rest_patch(TABLE_FILES, {"id": f"eq.{file_id}"}, patch, return_representation=False)
        return file_id

    inserted = _rest_insert(TABLE_FILES, row, return_representation=True)
    if inserted:
        return int(inserted[0]["id"])

    existing = _rest_select(TABLE_FILES, {"select": "id", "file_url": f"eq.{file_url}", "limit": "1"})
    return int(existing[0]["id"]) if existing else None


def _pick_best_metric_values(metric_sources: Dict[Tuple[str, str], List[Dict[str, Any]]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    best: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for key, candidates in metric_sources.items():
        valid = [c for c in candidates if c.get("value") is not None]
        if not valid:
            continue
        valid = sorted(valid, key=lambda c: (float(c.get("confidence") or 0), len(str(c.get("raw_text") or ""))), reverse=True)
        best[key] = valid[0]
    return best


def save_metrics_for_report(
    annual_report_id: int,
    issuer: str,
    issuer_norm: str,
    report_year: Optional[int],
    published_at: Any,
    best_metrics: Dict[Tuple[str, str], Dict[str, Any]],
) -> int:
    if not annual_report_id:
        return 0

    # Perrašome tos pačios ataskaitos rodiklius, kad blogi ankstesni bandymai būtų pakeisti.
    _rest_delete(TABLE_METRICS, {"annual_report_id": f"eq.{annual_report_id}"})

    saved = 0
    for (metric_name, metric_group), info in best_metrics.items():
        value = info.get("value")
        if value is None:
            continue
        row = {
            "annual_report_id": annual_report_id,
            "annual_report_file_id": info.get("annual_report_file_id"),
            "issuer": issuer,
            "issuer_norm": issuer_norm,
            "report_year": report_year,
            "published_at": _to_iso(published_at),
            "metric_name": metric_name,
            "metric_group": metric_group,
            "metric_value": value,
            "metric_unit": "tūkst. EUR",
            "source_type": info.get("source"),
            "source_file_url": info.get("file_url"),
            "source_storage_path": None,
            "parse_status": ((info.get("note") or "parsed") + ("; " + str(info.get("unit_note")) if info.get("unit_note") else ""))[:1000],
        }
        _rest_insert(TABLE_METRICS, row, return_representation=False)
        saved += 1
    return saved


# ============================================================
# ATASKAITŲ APDOROJIMAS
# ============================================================


def _is_technical_zip_entry(name: str) -> bool:
    lower = name.lower().replace("\\", "/")
    base = os.path.basename(lower)
    if lower.startswith("meta-inf/") or "/meta-inf/" in lower:
        return True
    if lower.startswith("_rels/") or "/_rels/" in lower:
        return True
    if base in {"catalog.xml", "taxonomy-package.xml", "taxonomypackage.xml"}:
        return True
    if lower.endswith(".xsd"):
        return True
    # ESEF taksonomijos linkbase failai, ne pati ataskaita.
    if re.search(r"(_lab|_pre|_cal|_def|_ref|_gen)[-_a-z0-9]*\.xml$", lower):
        return True
    return False


def _zip_entry_score(name: str, data: bytes) -> int:
    lower = name.lower().replace("\\", "/")
    base = os.path.basename(lower)
    sample = (data or b"")[:4000].lower()
    score = 100
    if _is_technical_zip_entry(name):
        return 999
    if "/reports/" in lower or lower.startswith("reports/"):
        score -= 45
    if lower.endswith((".xhtml", ".html", ".htm")):
        score -= 35
    if lower.endswith((".xbrl", ".xml")):
        score -= 20
    if lower.endswith(".pdf"):
        score -= 10
    if b"ix:nonfraction" in sample or b"ix:non-numeric" in sample or b"xbrli:xbrl" in sample:
        score -= 40
    if any(x in lower for x in ["annual", "metin", "financial", "finansin", "ataskait", "report", "ifrs", "conso"]):
        score -= 15
    if any(x in lower for x in ["presentation", "pristat", "sprendim", "balsavimo", "auditor", "opinion", "isvada"]):
        score += 25
    # Dideli PDF dažniau yra pati ataskaita, ne auditoriaus išvada.
    if lower.endswith(".pdf") and len(data or b"") > 1_000_000:
        score -= 8
    return score


def _iter_zip_members(content: bytes) -> Iterable[Tuple[str, bytes, str]]:
    candidates: List[Tuple[int, str, bytes, str]] = []
    with zipfile.ZipFile(BytesIO(content)) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            lower = name.lower()
            if not lower.endswith(ALLOWED_ATTACHMENT_EXTENSIONS):
                continue
            if _is_technical_zip_entry(name):
                continue
            try:
                data = zf.read(name)
            except Exception:
                continue
            if not data or len(data) < 50:
                continue
            ftype = _detect_file_type(data, name=name)
            if ftype == "unknown":
                if lower.endswith(".pdf"):
                    ftype = "pdf"
                elif lower.endswith(".zip"):
                    ftype = "zip"
                elif lower.endswith((".xhtml", ".html", ".htm")):
                    ftype = "xhtml"
                elif lower.endswith((".xml", ".xbrl")):
                    ftype = "xml"
            candidates.append((_zip_entry_score(name, data), name, data, ftype))

    for _, name, data, ftype in sorted(candidates, key=lambda x: x[0]):
        yield name, data, ftype


def _merge_metric_sources(
    metric_sources: Dict[Tuple[str, str], List[Dict[str, Any]]],
    metrics: Dict[Tuple[str, str], Dict[str, Any]],
    annual_report_file_id: Optional[int],
    file_url: str,
):
    for key, info in metrics.items():
        item = dict(info)
        item["annual_report_file_id"] = annual_report_file_id
        item["file_url"] = file_url
        metric_sources[key].append(item)


def update_annual_reports_metrics(start_date: date, end_date: date, max_reports: int = 300, progress=None) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "module_version": MODULE_VERSION,
        "allowed_issuers": 0,
        "annual_news_found": 0,
        "annual_news_processed": 0,
        "reports_saved": 0,
        "attachments_found": 0,
        "attachments_downloaded": 0,
        "files_saved": 0,
        "zip_members_processed": 0,
        "raw_text_saved": 0,
        "metrics_found": 0,
        "metrics_saved": 0,
        "errors": 0,
        "error_examples": [],
        "last_messages": [],
    }

    def log(message: str):
        stats["last_messages"].append(message)
        stats["last_messages"] = stats["last_messages"][-30:]
        if progress:
            progress(message)

    allowed = load_vln_official_secondary_issuers()
    stats["allowed_issuers"] = int(len(allowed)) if allowed is not None else 0

    news_df = load_annual_crib_news(start_date, end_date, allowed)
    if news_df is None or news_df.empty:
        return stats

    stats["annual_news_found"] = int(len(news_df))
    news_df = news_df.head(int(max_reports)).copy()

    for _, news in news_df.iterrows():
        issuer = _collapse_ws(news.get("issuer"))
        issuer_norm = _issuer_key(issuer)
        title = _collapse_ws(news.get("title"))
        crib_url = _collapse_ws(news.get("crib_url"))
        report_year = _safe_int(news.get("report_year"))
        published_at = news.get("published_at")

        stats["annual_news_processed"] += 1
        log(f"Tikrinama: {issuer} | {title[:120]}")

        try:
            annual_report_id = ensure_annual_report(news)
            if not annual_report_id:
                continue
            stats["reports_saved"] += 1

            metric_sources: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)

            attachments = get_crib_attachment_items(crib_url, content_hint=str(news.get("content") or ""))
            stats["attachments_found"] += len(attachments)

            # Jei priedų nėra, bent jau parsininame patį CRIB pranešimo tekstą.
            if not attachments:
                content_text = _collapse_ws(news.get("content"))
                content_bytes = content_text.encode("utf-8")
                metrics = _extract_metrics_from_text_lines(content_text)
                file_url = f"{crib_url}#content"
                file_id = ensure_annual_report_file(
                    annual_report_id=annual_report_id,
                    issuer=issuer,
                    report_year=report_year,
                    file_url=file_url,
                    file_name="CRIB pranešimo tekstas",
                    file_type="text",
                    content=content_bytes,
                    raw_text=content_text,
                    parse_status="parsed_crib_content" if metrics else "parsed_crib_content_no_metrics",
                )
                stats["files_saved"] += 1 if file_id else 0
                stats["raw_text_saved"] += 1 if content_text else 0
                _merge_metric_sources(metric_sources, metrics, file_id, file_url)

            for attachment in attachments[:12]:
                attachment_url = attachment.get("url") or ""
                attachment_name = attachment.get("name") or _file_name_from_url(attachment_url)
                try:
                    content, file_type, ctype = download_attachment(attachment_url, name=attachment_name)
                    if not content or len(content) < 50:
                        continue
                    stats["attachments_downloaded"] += 1

                    if file_type == "zip":
                        # Saugojame patį ZIP kaip failą, bet metrics imame iš vidinių PDF/XBRL.
                        zip_file_id = ensure_annual_report_file(
                            annual_report_id=annual_report_id,
                            issuer=issuer,
                            report_year=report_year,
                            file_url=attachment_url,
                            file_name=attachment_name or _file_name_from_url(attachment_url, "report.zip"),
                            file_type="zip",
                            content=content,
                            raw_text="",
                            parse_status="zip_downloaded",
                        )
                        stats["files_saved"] += 1 if zip_file_id else 0

                        try:
                            for inner_name, inner_content, inner_type in _iter_zip_members(content):
                                if inner_type not in {"pdf", "xhtml", "html", "xml", "xbrl"}:
                                    continue
                                stats["zip_members_processed"] += 1
                                inner_url = f"{attachment_url}#inner={quote(inner_name)}"
                                metrics, raw_text, diag = parse_any_content(inner_content, inner_type, file_name=inner_name)
                                file_id = ensure_annual_report_file(
                                    annual_report_id=annual_report_id,
                                    issuer=issuer,
                                    report_year=report_year,
                                    file_url=inner_url,
                                    file_name=inner_name,
                                    file_type=inner_type,
                                    content=inner_content,
                                    raw_text=raw_text,
                                    parse_status=str(diag.get("parse_status") or "parsed_inner"),
                                )
                                stats["files_saved"] += 1 if file_id else 0
                                stats["raw_text_saved"] += 1 if raw_text else 0
                                _merge_metric_sources(metric_sources, metrics, file_id, inner_url)
                        except Exception as zip_exc:
                            stats["errors"] += 1
                            stats["error_examples"].append(f"ZIP klaida {attachment_name}: {zip_exc}")
                        continue

                    # Tiesioginis PDF / XHTML / XML.
                    metrics, raw_text, diag = parse_any_content(content, file_type, file_name=attachment_name)
                    file_id = ensure_annual_report_file(
                        annual_report_id=annual_report_id,
                        issuer=issuer,
                        report_year=report_year,
                        file_url=attachment_url,
                        file_name=attachment_name or _file_name_from_url(attachment_url),
                        file_type=file_type,
                        content=content,
                        raw_text=raw_text,
                        parse_status=str(diag.get("parse_status") or "parsed"),
                    )
                    stats["files_saved"] += 1 if file_id else 0
                    stats["raw_text_saved"] += 1 if raw_text else 0
                    _merge_metric_sources(metric_sources, metrics, file_id, attachment_url)

                except Exception as attachment_exc:
                    stats["errors"] += 1
                    msg = f"Priedo klaida {issuer} | {attachment_name}: {attachment_exc}"
                    stats["error_examples"].append(msg[:1000])
                    stats["error_examples"] = stats["error_examples"][-20:]
                    continue

            # Fallback: jeigu failai rodiklių nedavė, pabandom CRIB content.
            if not metric_sources:
                content_text = _collapse_ws(news.get("content"))
                metrics = _extract_metrics_from_text_lines(content_text)
                if metrics:
                    file_url = f"{crib_url}#content"
                    content_bytes = content_text.encode("utf-8")
                    file_id = ensure_annual_report_file(
                        annual_report_id=annual_report_id,
                        issuer=issuer,
                        report_year=report_year,
                        file_url=file_url,
                        file_name="CRIB pranešimo tekstas",
                        file_type="text",
                        content=content_bytes,
                        raw_text=content_text,
                        parse_status="parsed_crib_content",
                    )
                    stats["files_saved"] += 1 if file_id else 0
                    stats["raw_text_saved"] += 1 if content_text else 0
                    _merge_metric_sources(metric_sources, metrics, file_id, file_url)

            best = _pick_best_metric_values(metric_sources)
            stats["metrics_found"] += len(best)
            if best:
                saved = save_metrics_for_report(
                    annual_report_id=annual_report_id,
                    issuer=issuer,
                    issuer_norm=issuer_norm,
                    report_year=report_year,
                    published_at=published_at,
                    best_metrics=best,
                )
                stats["metrics_saved"] += saved
                log(f"Įrašyta rodiklių: {issuer} | {saved}")
            else:
                # Išvalome senus šios ataskaitos rodiklius tik tada, kai visai nieko neradome? Ne.
                # Paliekame senus, jei tokie buvo, kad nepablogintume DB.
                log(f"Rodiklių nerasta: {issuer} | {title[:100]}")

        except Exception as exc:
            stats["errors"] += 1
            msg = f"Ataskaitos klaida {issuer} | {title[:80]}: {exc}"
            stats["error_examples"].append(msg[:1000])
            stats["error_examples"] = stats["error_examples"][-20:]
            log(msg[:300])
            continue

    return stats


# ============================================================
# DUOMENŲ NUSKAITYMAS IR ATVAIZDAVIMAS
# ============================================================


def load_annual_metrics_from_db(start_date: date, end_date: date) -> pd.DataFrame:
    try:
        start_iso = f"{start_date}T00:00:00"
        end_iso = f"{end_date}T23:59:59"
        rows = _rest_select(
            TABLE_METRICS,
            {
                "select": "*",
                "published_at": [f"gte.{start_iso}", f"lte.{end_iso}"],
                "order": "published_at.desc",
                "limit": "10000",
            },
        )
        return pd.DataFrame(rows)
    except Exception as exc:
        st.error(f"Nepavyko nuskaityti `{TABLE_METRICS}`: {exc}")
        return pd.DataFrame()


def load_annual_reports_overview(start_date: date, end_date: date) -> Tuple[pd.DataFrame, pd.DataFrame]:
    try:
        start_iso = f"{start_date}T00:00:00"
        end_iso = f"{end_date}T23:59:59"
        reports = _rest_select(
            TABLE_REPORTS,
            {
                "select": "*",
                "published_at": [f"gte.{start_iso}", f"lte.{end_iso}"],
                "order": "published_at.desc",
                "limit": "10000",
            },
        )
        files = _rest_select(
            TABLE_FILES,
            {
                "select": "*",
                "order": "created_at.desc",
                "limit": "10000",
            },
        )
        return pd.DataFrame(reports), pd.DataFrame(files)
    except Exception:
        return pd.DataFrame(), pd.DataFrame()


def _prepare_metrics_display_df(metrics_df: pd.DataFrame) -> pd.DataFrame:
    if metrics_df is None or metrics_df.empty:
        return pd.DataFrame()

    df = metrics_df.copy()
    for col in ["issuer", "report_year", "published_at", "metric_name", "metric_group", "metric_value", "parse_status", "source_file_url"]:
        if col not in df.columns:
            df[col] = None

    df["metric_label"] = df["metric_name"].fillna("").astype(str) + " - " + df["metric_group"].fillna("").astype(str)
    df["published_date"] = pd.to_datetime(df["published_at"], errors="coerce").dt.date

    index_cols = ["issuer", "report_year", "published_date", "annual_report_id"]
    for col in index_cols:
        if col not in df.columns:
            df[col] = None

    pivot = (
        df.pivot_table(
            index=index_cols,
            columns="metric_label",
            values="metric_value",
            aggfunc="first",
        )
        .reset_index()
    )
    pivot.columns = [str(c) for c in pivot.columns]

    # Tvarkinga stulpelių seka.
    desired_metric_cols = []
    for metric in METRIC_ORDER:
        for group in GROUP_ORDER:
            label = f"{metric} - {group}"
            if label in pivot.columns:
                desired_metric_cols.append(label)

    front = ["issuer", "report_year", "published_date"]
    tail = [c for c in pivot.columns if c not in set(front + ["annual_report_id"] + desired_metric_cols)]
    out = pivot[front + desired_metric_cols + tail + (["annual_report_id"] if "annual_report_id" in pivot.columns else [])].copy()

    out = out.rename(columns={
        "issuer": "Emitentas",
        "report_year": "Metai",
        "published_date": "Paskelbimo data",
        "Turtas - Grupė": "Turtas, tūkst. EUR - Grupė",
        "Turtas - Bendrovė": "Turtas, tūkst. EUR - Bendrovė",
        "Turtas - Neatskirta": "Turtas, tūkst. EUR - Neatskirta",
        "Nuosavas kapitalas - Grupė": "Nuosavas kapitalas, tūkst. EUR - Grupė",
        "Nuosavas kapitalas - Bendrovė": "Nuosavas kapitalas, tūkst. EUR - Bendrovė",
        "Nuosavas kapitalas - Neatskirta": "Nuosavas kapitalas, tūkst. EUR - Neatskirta",
        "Grynasis pelnas - Grupė": "Grynasis pelnas, tūkst. EUR - Grupė",
        "Grynasis pelnas - Bendrovė": "Grynasis pelnas, tūkst. EUR - Bendrovė",
        "Grynasis pelnas - Neatskirta": "Grynasis pelnas, tūkst. EUR - Neatskirta",
        "Pajamos - Grupė": "Pajamos, tūkst. EUR - Grupė",
        "Pajamos - Bendrovė": "Pajamos, tūkst. EUR - Bendrovė",
        "Pajamos - Neatskirta": "Pajamos, tūkst. EUR - Neatskirta",
        "annual_report_id": "Ataskaitos ID",
    })
    return out


def _style_numeric(val):
    return ""


def _show_diagnostics(start_date: date, end_date: date):
    reports_df, files_df = load_annual_reports_overview(start_date, end_date)
    metrics_df = load_annual_metrics_from_db(start_date, end_date)

    st.markdown("### Diagnostika")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("annual_reports", len(reports_df) if reports_df is not None else 0)
    c2.metric("annual_report_files", len(files_df) if files_df is not None else 0)
    c3.metric("annual_report_metrics", len(metrics_df) if metrics_df is not None else 0)
    if files_df is not None and not files_df.empty and "raw_text" in files_df.columns:
        c4.metric("Failų su raw_text", int(files_df["raw_text"].fillna("").astype(str).str.len().gt(0).sum()))
    else:
        c4.metric("Failų su raw_text", 0)

    last_stats = st.session_state.get("metines_last_update_stats")
    if last_stats:
        st.markdown("**Paskutinio paleidimo rezultatas**")
        st.json(last_stats)
    else:
        st.info("Rodiklių ištraukimas šioje sesijoje dar nebuvo paleistas. Spausk mygtuką „📥 Atsisiųsti tekstą ir ištraukti rodiklius“.")

    if files_df is not None and not files_df.empty:
        with st.expander("annual_report_files pavyzdžiai", expanded=False):
            view = files_df.copy()
            if "raw_text" in view.columns:
                view["raw_text_len"] = view["raw_text"].fillna("").astype(str).str.len()
            cols = [c for c in ["id", "annual_report_id", "issuer", "report_year", "file_name", "file_type", "file_size", "parse_status", "raw_text_len", "file_url"] if c in view.columns]
            st.dataframe(view[cols].head(50), use_container_width=True, hide_index=True)

    if metrics_df is not None and not metrics_df.empty:
        with st.expander("annual_report_metrics žali duomenys", expanded=False):
            st.dataframe(metrics_df.head(200), use_container_width=True, hide_index=True)


# ============================================================
# STREAMLIT PUSLAPIS
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
                        CRIB kategorijos „Metinė informacija“ dokumentai. Modulis išsaugo ataskaitų tekstą,
                        ištraukia balanso ir pelno (nuostolių) rodiklius ir įrašo juos į annual_report_metrics.
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"Versija: {MODULE_VERSION}")
    st.markdown("<br>", unsafe_allow_html=True)

    with st.sidebar:
        st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
        st.markdown('<div class="sidebar-card-title">📚 Metinės ataskaitos</div>', unsafe_allow_html=True)
        start_date = st.date_input(
            "Metinės informacijos data nuo",
            value=date.today() - timedelta(days=730),
            key="metines_start_date",
        )
        end_date = st.date_input(
            "Metinės informacijos data iki",
            value=date.today(),
            key="metines_end_date",
        )
        max_reports = st.number_input(
            "Maks. CRIB pranešimų vienu paleidimu",
            min_value=10,
            max_value=1000,
            value=300,
            step=10,
            key="metines_max_reports",
        )
        update_btn = st.button(
            "📥 Atsisiųsti tekstą ir ištraukti rodiklius",
            use_container_width=True,
            type="primary",
            key="metines_update_btn",
        )
        st.markdown("</div>", unsafe_allow_html=True)

    if start_date > end_date:
        st.error("Data „nuo“ negali būti vėlesnė už datą „iki“.")
        st.stop()

    if update_btn:
        progress_box = st.empty()

        def progress(msg: str):
            progress_box.info(msg)

        try:
            with st.spinner("Atsisiunčiamos metinės ataskaitos, traukiamas tekstas ir rodikliai..."):
                stats = update_annual_reports_metrics(
                    start_date=start_date,
                    end_date=end_date,
                    max_reports=int(max_reports),
                    progress=progress,
                )
            st.session_state["metines_last_update_stats"] = stats
            progress_box.success(
                "Baigta: "
                f"rasta pranešimų {stats.get('annual_news_found', 0)}, "
                f"apdorota {stats.get('annual_news_processed', 0)}, "
                f"priedų rasta {stats.get('attachments_found', 0)}, "
                f"failų išsaugota {stats.get('files_saved', 0)}, "
                f"rodiklių rasta {stats.get('metrics_found', 0)}, "
                f"rodiklių įrašyta {stats.get('metrics_saved', 0)}, "
                f"klaidų {stats.get('errors', 0)}."
            )
            st.rerun()
        except Exception as exc:
            st.error("Nepavyko atnaujinti metinių ataskaitų rodiklių.")
            st.exception(exc)
            st.stop()

    with st.expander("Diagnostika", expanded=True):
        _show_diagnostics(start_date, end_date)

    metrics_df = load_annual_metrics_from_db(start_date, end_date)
    display_df = _prepare_metrics_display_df(metrics_df)

    if display_df.empty:
        st.info("Pasirinktu laikotarpiu metinių ataskaitų rodiklių duomenų bazėje nėra.")
        st.markdown(
            """
            <div class="info-box">
                Rodiklių lentelė tuščia, nes annual_report_metrics dar neturi įrašų šiam laikotarpiui.
                Spausk kairėje esantį mygtuką „📥 Atsisiųsti tekstą ir ištraukti rodiklius“.
                Diagnostikoje matysi, ar rasti CRIB pranešimai, ar atsisiųsti failai ir ar Supabase priėmė INSERT.
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Ataskaitų", display_df["Ataskaitos ID"].nunique() if "Ataskaitos ID" in display_df.columns else len(display_df))
    c2.metric("Emitentų", display_df["Emitentas"].replace("", pd.NA).dropna().nunique() if "Emitentas" in display_df.columns else 0)
    c3.metric("Rodiklių eilučių DB", len(metrics_df))
    c4.metric("Laikotarpis", f"{start_date} – {end_date}")

    st.markdown("---")
    with st.expander("Filtrai", expanded=True):
        filtered = display_df.copy()
        if "Emitentas" in filtered.columns:
            issuers = sorted(filtered["Emitentas"].dropna().astype(str).unique())
            selected = st.multiselect("Emitentas", issuers, key="metines_filter_issuer")
            if selected:
                filtered = filtered[filtered["Emitentas"].isin(selected)]
        if "Metai" in filtered.columns:
            years = sorted([int(y) for y in filtered["Metai"].dropna().unique() if str(y).strip()])
            selected_years = st.multiselect("Metai", years, key="metines_filter_year")
            if selected_years:
                filtered = filtered[filtered["Metai"].isin(selected_years)]

    st.subheader("Metinių ataskaitų rodiklių lentelė")

    # Skaitinių stulpelių formatavimas.
    format_map = {}
    for col in filtered.columns:
        if "tūkst. EUR" in col:
            format_map[col] = "{:,.0f}"

    styler = filtered.style
    if format_map:
        styler = styler.format(format_map, na_rep="")
    st.dataframe(styler, use_container_width=True, hide_index=True)

    st.download_button(
        "⬇ Atsisiųsti CSV",
        data=filtered.to_csv(index=False).encode("utf-8-sig"),
        file_name="metiniu_ataskaitu_rodikliai.csv",
        mime="text/csv",
        use_container_width=True,
    )


# Suderinamumo aliasai app.py importams.
show_annual_reports_page = show_metines_page
show_annual_reports_metrics_page = show_metines_page


if __name__ == "__main__":
    # Minimalus lokalaus importo testas.
    print(MODULE_VERSION)

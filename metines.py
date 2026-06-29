
# -*- coding: utf-8 -*-
"""
Metiniu ataskaitu modulis Rinkos pulsui.

Tikslas:
- is Supabase market_news lenteles paimti CRIB kategorijos "Metine informacija" pranesimus;
- atrinkti tik Supabase market_issuers sarase saugomus VLN emitentus;
- is CRIB priedu atsisiusti PDF arba XBRL metines ataskaitas;
- istraukti pagrindinius finansinius rodiklius:
    * Balansinis turtas, tukst. EUR: grupes ir bendroves;
    * Pajamos, tukst. EUR: grupes ir bendroves;
- rezultatus issaugoti Supabase lenteleje annual_report_metrics;
- parodyti atskira Streamlit ataskaitos puslapi.

Pastaba: PDF lenteliu struktura tarp emitentu skiriasi, todel parseris yra atsargus.
Jeigu rodiklio nepavyksta patikimai nustatyti, laukas paliekamas tuscias, o parse_status
ir parse_note parodo priezasti.
"""

import os
import re
import hashlib
import warnings
from datetime import date, timedelta
from io import BytesIO
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import pandas as pd
import requests
import streamlit as st
import pdfplumber
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=UserWarning)

try:
    from supabase_cache import load_news_df
except Exception:
    load_news_df = None


ANNUAL_CATEGORY_TOKENS = (
    "metinė informacija",
    "metine informacija",
    "annual information",
)

OFFICIAL_OR_SECONDARY_TOKENS = (
    "oficial", "official", "main list", "baltic main", "papild", "secondary", "additional",
)

EXCLUDED_LIST_TOKENS = (
    "first north", "bond", "oblig", "fund", "etf", "vyriausyb", "government",
)

METRICS_TABLE = "annual_report_metrics"


# ------------------------------------------------------------
# Bendros pagalbines funkcijos
# ------------------------------------------------------------

def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm(value: str) -> str:
    s = str(value or "").lower().strip()
    repl = str.maketrans({"ą": "a", "č": "c", "ę": "e", "ė": "e", "į": "i", "š": "s", "ų": "u", "ū": "u", "ž": "z"})
    s = s.translate(repl)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _issuer_key(value: str) -> str:
    s = _norm(value)
    s = re.sub(r"\b(ab|uab|as|asa|akcine bendrove|uzdaroji akcine bendrove)\b", " ", s)
    s = s.replace(" group", " ").replace(" grupe", " ")
    return re.sub(r"\s+", " ", s).strip()


def _parse_number(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null", "-", "–"}:
        return None
    neg = False
    if re.search(r"\(.*\)", s):
        neg = True
    s = s.replace("\u00a0", " ")
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
        out = float(num)
        if neg and out > 0:
            out = -out
        return out
    except Exception:
        return None


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content or b"").hexdigest()


def _to_iso(value):
    try:
        if value is not None and not pd.isna(value):
            return pd.to_datetime(value).isoformat()
    except Exception:
        pass
    return None


def _supabase_client_parts():
    from supabase_cache import _supabase_headers, _supabase_rest_url, _http_client
    return _supabase_headers, _supabase_rest_url, _http_client


# ------------------------------------------------------------
# Emitentai: tik Vilniaus oficialusis ir papildomasis sarasas
# ------------------------------------------------------------

def load_vln_official_secondary_issuers() -> pd.DataFrame:
    """Uzkrauna leidziamu emitentu sarasa is Supabase market_issuers.

    Svarbu: emitentu sarasas jau yra pildomas duomenu bazeje, todel cia
    nebespeliiojame pagal CRIB ir papildomai nesurenkame emitentu is isores.
    Imami tik market_issuers irasai su market='VLN'.

    Jeigu market_issuers lenteleje jau laikomi tik Vilniaus oficialiojo ir
    papildomojo saraso emitentai, sis filtras bus tiksliai toks, kokio reikia.
    Jeigu lenteleje yra ir First North / obligaciju / fondu irasu, jie bus
    atmesti tik pagal aiskiai matomus segmento / tipo pozymius DB laukuose.
    """
    try:
        headers, rest_url, http_client = _supabase_client_parts()
        url = rest_url("market_issuers")
        params = {"select": "*", "market": "eq.VLN", "order": "issuer.asc"}
        with http_client() as client:
            resp = client.get(url, headers=headers(), params=params)
            resp.raise_for_status()
            rows = resp.json() or []
    except Exception as exc:
        st.warning(f"Nepavyko uzkrauti market_issuers lenteles: {exc}")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    if "issuer" not in df.columns:
        if "company" in df.columns:
            df["issuer"] = df["company"]
        else:
            df["issuer"] = ""

    # Jei DB turi aktyvumo pozymi, paliekame tik aktyvius irasus.
    for active_col in ["is_active", "active", "listed"]:
        if active_col in df.columns:
            mask = df[active_col].fillna(True).astype(str).str.lower().isin(["true", "1", "yes", "taip"])
            if mask.any():
                df = df[mask].copy()
            break

    # Neatmetame emitentu pagal pavadinimo interpretacijas, bet jei tame paciame
    # Supabase sarase yra obligaciju / fondu / First North instrumentu, juos
    # isfiltruojame pagal aiskiai DB saugomus segmentu ar tipo laukus.
    searchable_cols = [c for c in df.columns if c.lower() in {
        "list", "listing", "segment", "market_segment", "market_list", "trading_list",
        "board", "instrument_group", "security_type", "asset_class", "category", "group", "type",
    }]
    if searchable_cols:
        text = df[searchable_cols].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        excluded = text.apply(lambda x: any(t in x for t in EXCLUDED_LIST_TOKENS))
        df = df[~excluded].copy()

    df["issuer"] = df["issuer"].fillna("").astype(str).str.strip()
    df = df[df["issuer"] != ""].copy()

    # Naudojame DB normalizuotus laukus, jei jie yra, o jei ne - pasidarome lokaliai.
    if "issuer_norm" in df.columns:
        norm_from_db = df["issuer_norm"].fillna("").astype(str).str.strip()
    else:
        norm_from_db = pd.Series([""] * len(df), index=df.index)
    df["issuer_key"] = norm_from_db.where(norm_from_db.ne(""), df["issuer"].apply(_issuer_key))
    df["issuer_key"] = df["issuer_key"].apply(_issuer_key)

    keep_cols = [c for c in ["issuer", "company", "ticker", "isin", "issuer_norm", "company_norm", "issuer_key"] if c in df.columns]
    return df[keep_cols].drop_duplicates(subset=["issuer_key"]).reset_index(drop=True)

def _canonical_issuer_from_allowed(value: str, allowed_issuers: pd.DataFrame) -> str:
    if allowed_issuers is None or allowed_issuers.empty:
        return str(value or "").strip()
    key = _issuer_key(value)
    if not key:
        return ""
    lookup = dict(zip(allowed_issuers["issuer_key"], allowed_issuers["issuer"]))
    if key in lookup:
        return lookup[key]
    for k, issuer in lookup.items():
        if key and k and (key in k or k in key):
            return issuer
    return ""


def _infer_issuer_from_text(title: str, content: str, allowed_issuers: pd.DataFrame) -> str:
    if allowed_issuers is None or allowed_issuers.empty:
        return ""
    text_key = _issuer_key(f"{title or ''} {content or ''}")
    best = ""
    best_len = 0
    for _, row in allowed_issuers.iterrows():
        key = str(row.get("issuer_key") or "")
        issuer = str(row.get("issuer") or "")
        if len(key) < 3:
            continue
        if re.search(rf"(?:^|\s){re.escape(key)}(?:\s|$)", text_key):
            if len(key) > best_len:
                best = issuer
                best_len = len(key)
    return best


# ------------------------------------------------------------
# CRIB metiniu pranesimu ir priedu nuskaitymas
# ------------------------------------------------------------

def _is_annual_information_row(row) -> bool:
    text = " ".join([
        str(row.get("category", "") or ""),
        str(row.get("title", "") or ""),
        str(row.get("content", "") or "")[:500],
    ]).lower()
    return any(token in text for token in ANNUAL_CATEGORY_TOKENS)


def load_annual_crib_news(start_date, end_date, allowed_issuers: pd.DataFrame) -> pd.DataFrame:
    if load_news_df is None:
        st.error("Nerasta supabase_cache.load_news_df funkcijos.")
        return pd.DataFrame()

    df = load_news_df("crib", start_date, end_date)
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    for col in ["company", "category", "title", "published_at", "url", "content"]:
        if col not in df.columns:
            df[col] = ""

    df = df[df.apply(_is_annual_information_row, axis=1)].copy()
    if df.empty:
        return df

    df["issuer"] = df["company"].fillna("").astype(str).str.strip().apply(
        lambda x: _canonical_issuer_from_allowed(x, allowed_issuers)
    )
    missing = df["issuer"].fillna("").astype(str).str.strip().eq("")
    if missing.any():
        df.loc[missing, "issuer"] = df.loc[missing].apply(
            lambda r: _infer_issuer_from_text(r.get("title", ""), r.get("content", ""), allowed_issuers),
            axis=1,
        )

    df = df[df["issuer"].fillna("").astype(str).str.strip().ne("")].copy()
    df["issuer_key"] = df["issuer"].apply(_issuer_key)
    df["crib_url"] = df["url"].fillna("").astype(str).str.strip()
    df["published_at_dt"] = pd.to_datetime(df["published_at"], errors="coerce")
    df = df.sort_values("published_at_dt", ascending=False)
    df = df.drop_duplicates(subset=["crib_url"], keep="first")
    return df.reset_index(drop=True)


def _extract_attachment_links_from_html(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = (a.get_text(" ", strip=True) or "").lower()
        href_l = href.lower()
        if (
            ".pdf" in href_l or ".xhtml" in href_l or ".html" in href_l or ".xml" in href_l or ".zip" in href_l
            or "viewattachment.action" in href_l or "download" in href_l or "attachment" in href_l
            or "pdf" in text or "xbrl" in text or "xhtml" in text
        ):
            full = urljoin(base_url, href)
            if full not in out:
                out.append(full)
    return _rank_attachment_links(out)


def _rank_attachment_links(links: list[str]) -> list[str]:
    def score(u: str) -> int:
        u_l = u.lower()
        if "viewattachment" in u_l and (".xhtml" in u_l or ".xml" in u_l or "xbrl" in u_l):
            return 0
        if "viewattachment" in u_l and ".pdf" in u_l:
            return 1
        if "viewattachment" in u_l:
            return 2
        if ".xhtml" in u_l or ".xml" in u_l or "xbrl" in u_l:
            return 3
        if ".pdf" in u_l:
            return 4
        if "globenewswire" in u_l:
            return 9
        return 5
    return sorted(list(dict.fromkeys(links or [])), key=score)


def get_crib_attachment_links(crib_url: str) -> list[str]:
    if not crib_url:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        resp = requests.get(crib_url, headers=headers, verify=False, timeout=30)
        resp.raise_for_status()
        return _extract_attachment_links_from_html(resp.text, crib_url)
    except Exception:
        return []


def download_attachment(url: str) -> tuple[bytes, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/pdf,application/xhtml+xml,application/xml,text/html,application/zip,*/*",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(url, headers=headers, verify=False, timeout=60, allow_redirects=True)
    resp.raise_for_status()
    content = resp.content or b""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if content[:20].lstrip().startswith(b"%PDF"):
        return content, "pdf"
    sample = content[:500].lower()
    if b"xbrl" in sample or b"<html" in sample or b"<xhtml" in sample or b"<?xml" in sample:
        return content, "xbrl"
    if "pdf" in ctype:
        return content, "pdf"
    if "xml" in ctype or "html" in ctype or "xbrl" in ctype:
        return content, "xbrl"
    return content, "unknown"


# ------------------------------------------------------------
# PDF parseris
# ------------------------------------------------------------

ASSETS_LABELS = (
    "balansinis turtas", "turtas is viso", "turtas iš viso", "total assets", "assets total",
)
REVENUE_LABELS = (
    "pajamos", "pardavimo pajamos", "revenue", "sales revenue", "sales",
)


def _extract_pdf_text_and_tables(content: bytes):
    text_parts = []
    tables = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for page in pdf.pages:
            try:
                txt = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            except Exception:
                txt = page.extract_text() or ""
            if txt:
                text_parts.append(txt)
            try:
                for tbl in page.extract_tables() or []:
                    if tbl:
                        tables.append(tbl)
            except Exception:
                pass
    return "\n".join(text_parts), tables


def _row_text(row) -> str:
    return _norm(" ".join(str(c or "") for c in row))


def _numbers_from_row(row) -> list[float]:
    vals = []
    for cell in row:
        v = _parse_number(cell)
        if v is not None:
            vals.append(v)
    return vals


def _find_metric_in_tables(tables, label_tokens) -> tuple[float | None, float | None, str]:
    """Grazina (grupe, bendrove, note).

    Strategija:
    - ieskome eilutes, kurioje yra norimas rodiklis;
    - jei eiluteje yra bent 2 skaiciai, laikome, kad pirmi du yra "Grupes" ir "Bendroves";
    - jeigu yra 4 skaiciai, daznai tai einamieji ir palyginamieji metai; imame pirmus du.
    """
    for table in tables or []:
        for row in table or []:
            if not row:
                continue
            rt = _row_text(row)
            if any(_norm(label) in rt for label in label_tokens):
                nums = _numbers_from_row(row)
                if len(nums) >= 2:
                    return nums[0], nums[1], "reikšmės paimtos iš PDF lentelės eilutės"
                if len(nums) == 1:
                    return nums[0], None, "rasta tik viena reikšmė PDF lentelėje"
    return None, None, "rodiklio eilutė PDF lentelėse nerasta"


def _find_metric_in_text(text: str, label_tokens) -> tuple[float | None, float | None, str]:
    norm_text = _norm(text)
    original_lines = [line for line in (text or "").splitlines() if line.strip()]
    for line in original_lines:
        ln = _norm(line)
        if any(_norm(label) in ln for label in label_tokens):
            nums = [_parse_number(x) for x in re.findall(r"(?:\(?[-+]?\d[\d\s.,]*\)?)", line)]
            nums = [x for x in nums if x is not None]
            if len(nums) >= 2:
                return nums[0], nums[1], "reikšmės paimtos iš PDF teksto eilutės"
            if len(nums) == 1:
                return nums[0], None, "rasta tik viena reikšmė PDF tekste"
    if not norm_text:
        return None, None, "PDF tekstas tuščias"
    return None, None, "rodiklio eilutė PDF tekste nerasta"


def parse_pdf_annual_report(content: bytes) -> dict:
    text, tables = _extract_pdf_text_and_tables(content)
    assets_g, assets_c, assets_note = _find_metric_in_tables(tables, ASSETS_LABELS)
    revenue_g, revenue_c, revenue_note = _find_metric_in_tables(tables, REVENUE_LABELS)

    if assets_g is None and assets_c is None:
        assets_g, assets_c, assets_note = _find_metric_in_text(text, ASSETS_LABELS)
    if revenue_g is None and revenue_c is None:
        revenue_g, revenue_c, revenue_note = _find_metric_in_text(text, REVENUE_LABELS)

    year = None
    m = re.search(r"\b(20\d{2})\b", text or "")
    if m:
        year = int(m.group(1))

    found = sum(v is not None for v in [assets_g, assets_c, revenue_g, revenue_c])
    status = "parsed_pdf" if found else "parsed_pdf_no_metrics"
    return {
        "report_year": year,
        "assets_group_teu": assets_g,
        "assets_company_teu": assets_c,
        "revenue_group_teu": revenue_g,
        "revenue_company_teu": revenue_c,
        "parse_status": status,
        "parse_note": f"Turtas: {assets_note}; Pajamos: {revenue_note}",
        "raw_text": text[:12000] if text else "",
    }


# ------------------------------------------------------------
# XBRL parseris
# ------------------------------------------------------------

ASSETS_CONCEPTS = {
    "assets", "totalassets", "ifrsfullassets", "ifrs-fullassets",
}
REVENUE_CONCEPTS = {
    "revenue", "salesrevenue", "ifrsfullrevenue", "ifrs-fullrevenue",
    "revenuefromcontractswithcustomers", "revenuefromcontractswithcustomersexcludingassessedtax",
}


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag.split(":")[-1]


def _concept_key(tag: str) -> str:
    return _norm(_local_name(tag)).replace(" ", "")


def parse_xbrl_annual_report(content: bytes) -> dict:
    try:
        root = ET.fromstring(content)
    except Exception:
        # Kai kurie inline XBRL failai buna XHTML su neidealiu XML. Bandom minimaliai isvalyti.
        text = content.decode("utf-8", errors="ignore")
        text = re.sub(r"&nbsp;", " ", text)
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except Exception as exc:
            return {
                "report_year": None,
                "assets_group_teu": None,
                "assets_company_teu": None,
                "revenue_group_teu": None,
                "revenue_company_teu": None,
                "parse_status": "xbrl_parse_error",
                "parse_note": str(exc)[:500],
                "raw_text": "",
            }

    facts = []
    for elem in root.iter():
        key = _concept_key(elem.tag)
        if key not in ASSETS_CONCEPTS and key not in REVENUE_CONCEPTS:
            continue
        val = _parse_number(elem.text)
        if val is None:
            continue
        ctx = elem.attrib.get("contextRef") or elem.attrib.get("contextref") or ""
        unit = elem.attrib.get("unitRef") or elem.attrib.get("unitref") or ""
        decimals = elem.attrib.get("decimals", "")
        facts.append({"key": key, "value": val, "context": ctx, "unit": unit, "decimals": decimals})

    def pick(concepts):
        candidates = [f for f in facts if f["key"] in concepts]
        if not candidates:
            return None, None
        # Grupes ir bendroves atskyrimas XBRL yra emitentu-specifinis. Jei kontekstas turi
        # consolidated/group pozymi, ji laikome grupe. Jei separate/company pozymi - bendrove.
        group = [f for f in candidates if re.search(r"consolid|group|grupe|konsolid", f["context"], re.I)]
        company = [f for f in candidates if re.search(r"separate|company|bendrov|parent|individual", f["context"], re.I)]
        if group or company:
            g = group[0]["value"] if group else candidates[0]["value"]
            c = company[0]["value"] if company else None
            return g, c
        # Atsarginiu atveju pirmoji reiksme paliekama kaip grupes reiksme.
        return candidates[0]["value"], None

    assets_g, assets_c = pick(ASSETS_CONCEPTS)
    revenue_g, revenue_c = pick(REVENUE_CONCEPTS)

    found = sum(v is not None for v in [assets_g, assets_c, revenue_g, revenue_c])
    return {
        "report_year": None,
        "assets_group_teu": assets_g,
        "assets_company_teu": assets_c,
        "revenue_group_teu": revenue_g,
        "revenue_company_teu": revenue_c,
        "parse_status": "parsed_xbrl" if found else "parsed_xbrl_no_metrics",
        "parse_note": "XBRL/iXBRL faktai nuskaityti pagal IFRS taksonomijos pavadinimus; grupes/bendroves atskyrimas priklauso nuo contextRef.",
        "raw_text": "",
    }


# ------------------------------------------------------------
# Supabase issaugojimas
# ------------------------------------------------------------

def _metric_already_saved(crib_url: str, attachment_url: str) -> bool:
    try:
        headers, rest_url, http_client = _supabase_client_parts()
        url = rest_url(METRICS_TABLE)
        params = {"select": "id", "crib_url": f"eq.{crib_url}", "attachment_url": f"eq.{attachment_url}", "limit": "1"}
        with http_client() as client:
            resp = client.get(url, headers=headers(), params=params)
            resp.raise_for_status()
            return bool(resp.json() or [])
    except Exception:
        return False


def save_annual_metric_row(row: dict) -> bool:
    headers, rest_url, http_client = _supabase_client_parts()
    url = rest_url(METRICS_TABLE)
    clean = {k: v for k, v in row.items() if v is not pd.NA}
    with http_client() as client:
        resp = client.post(
            url,
            headers={**headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json=clean,
        )
        if resp.status_code in (200, 201, 204):
            return True
        if resp.status_code == 409:
            return False
        raise RuntimeError(f"Supabase {METRICS_TABLE} įrašymo klaida: {resp.status_code} - {resp.text}")


def load_annual_metrics_from_db(start_date, end_date) -> pd.DataFrame:
    try:
        headers, rest_url, http_client = _supabase_client_parts()
        url = rest_url(METRICS_TABLE)
        start_iso = f"{start_date}T00:00:00"
        end_iso = f"{end_date}T23:59:59"
        params = {
            "select": "*",
            "published_at": [f"gte.{start_iso}", f"lte.{end_iso}"],
            "order": "published_at.desc",
        }
        with http_client() as client:
            resp = client.get(url, headers=headers(), params=params)
            resp.raise_for_status()
            data = resp.json() or []
        return pd.DataFrame(data)
    except Exception as exc:
        st.error(f"Nepavyko nuskaityti {METRICS_TABLE}: {exc}")
        return pd.DataFrame()


# ------------------------------------------------------------
# Atnaujinimas
# ------------------------------------------------------------

def update_annual_reports_metrics(start_date, end_date, max_reports: int = 80, progress=None) -> dict:
    stats = {
        "annual_news_found": 0,
        "annual_news_processed": 0,
        "attachments_checked": 0,
        "rows_saved": 0,
        "skipped_existing": 0,
        "errors": 0,
    }

    allowed = load_vln_official_secondary_issuers()
    news_df = load_annual_crib_news(start_date, end_date, allowed)
    if news_df is None or news_df.empty:
        return stats

    news_df = news_df.head(max_reports).copy()
    stats["annual_news_found"] = len(news_df)

    for _, news in news_df.iterrows():
        crib_url = str(news.get("crib_url") or "").strip()
        if not crib_url:
            continue
        stats["annual_news_processed"] += 1
        if progress:
            progress(f"Tikrinama metinė ataskaita: {news.get('issuer', '')} - {news.get('title', '')}")

        try:
            links = get_crib_attachment_links(crib_url)
            if not links:
                continue
            for attachment_url in links[:5]:
                if _metric_already_saved(crib_url, attachment_url):
                    stats["skipped_existing"] += 1
                    continue
                stats["attachments_checked"] += 1
                content, file_type = download_attachment(attachment_url)
                if not content or len(content) < 100:
                    continue

                if file_type == "pdf":
                    parsed = parse_pdf_annual_report(content)
                elif file_type == "xbrl":
                    parsed = parse_xbrl_annual_report(content)
                else:
                    parsed = {
                        "report_year": None,
                        "assets_group_teu": None,
                        "assets_company_teu": None,
                        "revenue_group_teu": None,
                        "revenue_company_teu": None,
                        "parse_status": "unsupported_attachment_type",
                        "parse_note": "Priedas neatpažintas kaip PDF arba XBRL/iXBRL.",
                        "raw_text": "",
                    }

                row = {
                    "issuer": str(news.get("issuer") or ""),
                    "issuer_key": str(news.get("issuer_key") or ""),
                    "report_year": parsed.get("report_year"),
                    "published_at": _to_iso(news.get("published_at")),
                    "crib_url": crib_url,
                    "crib_title": str(news.get("title") or ""),
                    "crib_category": str(news.get("category") or ""),
                    "attachment_url": attachment_url,
                    "attachment_type": file_type,
                    "attachment_sha256": _sha256_bytes(content),
                    "assets_group_teu": parsed.get("assets_group_teu"),
                    "assets_company_teu": parsed.get("assets_company_teu"),
                    "revenue_group_teu": parsed.get("revenue_group_teu"),
                    "revenue_company_teu": parsed.get("revenue_company_teu"),
                    "parse_status": parsed.get("parse_status"),
                    "parse_note": parsed.get("parse_note"),
                    "raw_text": parsed.get("raw_text"),
                }
                if save_annual_metric_row(row):
                    stats["rows_saved"] += 1
        except Exception as exc:
            stats["errors"] += 1
            if progress:
                progress(f"Klaida: {exc}")
            continue

    return stats


# ------------------------------------------------------------
# Streamlit ataskaita
# ------------------------------------------------------------

def _prepare_display_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in [
        "issuer", "report_year", "published_at", "assets_group_teu", "assets_company_teu",
        "revenue_group_teu", "revenue_company_teu", "parse_status", "parse_note", "crib_title",
        "attachment_type", "crib_url", "attachment_url",
    ]:
        if col not in out.columns:
            out[col] = ""
    out["published_date"] = pd.to_datetime(out["published_at"], errors="coerce").dt.date
    rename = {
        "issuer": "Emitentas",
        "report_year": "Metai",
        "published_date": "Paskelbimo data",
        "assets_group_teu": "Balansinis turtas, tūkst. EUR - Grupės",
        "assets_company_teu": "Balansinis turtas, tūkst. EUR - Bendrovės",
        "revenue_group_teu": "Pajamos, tūkst. EUR - Grupės",
        "revenue_company_teu": "Pajamos, tūkst. EUR - Bendrovės",
        "parse_status": "Nuskaitymo statusas",
        "parse_note": "Pastaba",
        "crib_title": "CRIB pranešimas",
        "attachment_type": "Formatas",
        "crib_url": "CRIB nuoroda",
        "attachment_url": "Ataskaitos failas",
    }
    cols = list(rename.keys())
    cols = [c for c in cols if c in out.columns]
    out = out[cols].rename(columns=rename)
    return out


def show_annual_reports_page():
    st.markdown("# Metinių ataskaitų rodikliai")
    st.markdown(
        "CRIB kategorijos **Metinė informacija** PDF / XBRL ataskaitos. "
        "Rodoma tik tų emitentų informacija, kurie yra Supabase `market_issuers` sąraše su `market=VLN`."
    )

    with st.sidebar:
        st.markdown("### Metinės ataskaitos")
        start_date = st.date_input("Metinės informacijos data nuo", value=date.today() - timedelta(days=730), key="annual_reports_start_date")
        end_date = st.date_input("Metinės informacijos data iki", value=date.today(), key="annual_reports_end_date")
        max_reports = st.number_input("Maks. CRIB pranešimų", min_value=10, max_value=300, value=80, step=10, key="annual_reports_max_reports")
        update_btn = st.button("Atnaujinti metinių ataskaitų rodiklius", use_container_width=True, key="annual_reports_update_btn")

    if start_date > end_date:
        st.error("Data „nuo“ negali būti vėlesnė už datą „iki“.")
        st.stop()

    if update_btn:
        progress_box = st.empty()
        def progress(msg: str):
            progress_box.info(msg)
        try:
            with st.spinner("Nuskaitomos CRIB metinės ataskaitos ir pildoma duomenų bazė..."):
                stats = update_annual_reports_metrics(start_date, end_date, max_reports=int(max_reports), progress=progress)
            progress_box.success(
                "Atnaujinta: "
                f"rasta metinių pranešimų {stats.get('annual_news_found', 0)}, "
                f"apdorota {stats.get('annual_news_processed', 0)}, "
                f"patikrinta priedų {stats.get('attachments_checked', 0)}, "
                f"įrašyta {stats.get('rows_saved', 0)}, "
                f"praleista esamų {stats.get('skipped_existing', 0)}, "
                f"klaidų {stats.get('errors', 0)}."
            )
            st.rerun()
        except Exception as exc:
            st.error("Nepavyko atnaujinti metinių ataskaitų rodiklių.")
            st.exception(exc)
            st.stop()

    raw_df = load_annual_metrics_from_db(start_date, end_date)
    display_df = _prepare_display_df(raw_df)

    if display_df.empty:
        st.info("Pasirinktu laikotarpiu metinių ataskaitų rodiklių duomenų bazėje nėra.")
        st.stop()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Įrašų", len(display_df))
    c2.metric("Emitentų", display_df["Emitentas"].replace("", pd.NA).dropna().nunique())
    c3.metric("PDF", int((display_df["Formatas"] == "pdf").sum()))
    c4.metric("XBRL", int((display_df["Formatas"] == "xbrl").sum()))

    st.markdown("---")

    with st.expander("Filtrai", expanded=True):
        issuers = sorted(display_df["Emitentas"].dropna().astype(str).unique())
        selected = st.multiselect("Emitentas", issuers, key="annual_reports_filter_issuer")
        status_values = sorted(display_df["Nuskaitymo statusas"].dropna().astype(str).unique())
        statuses = st.multiselect("Nuskaitymo statusas", status_values, key="annual_reports_filter_status")
        if selected:
            display_df = display_df[display_df["Emitentas"].isin(selected)]
        if statuses:
            display_df = display_df[display_df["Nuskaitymo statusas"].isin(statuses)]

    st.subheader("Metinių ataskaitų rodiklių lentelė")
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    st.download_button(
        "Atsisiųsti CSV",
        data=display_df.to_csv(index=False).encode("utf-8-sig"),
        file_name="metiniu_ataskaitu_rodikliai.csv",
        mime="text/csv",
        use_container_width=True,
    )

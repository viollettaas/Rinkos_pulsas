# -*- coding: utf-8 -*-

import re
import os
import warnings
from datetime import date, timedelta
from io import BytesIO
from urllib.parse import urljoin

import requests
import pdfplumber
import pandas as pd
import urllib3
import streamlit as st
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=UserWarning)
os.environ.setdefault("WDM_SSL_VERIFY", "0")

from supabase_cache import load_news_df

try:
    from supabase_cache import load_manager_transactions_df
except Exception:
    load_manager_transactions_df = None



# ------------------------------------------------------------
# Vadovų sandorių atnaujinimas iš market_news CRIB pranešimų.
# Viskas šiame faile: nereikia manager_transactions_update.py ir
# nereikia backfill_manager_transactions_from_crib.py.
# ------------------------------------------------------------

MANAGER_CATEGORY_TOKENS = (
    "pranešimai apie vadovų sandorius",
    "pranesimai apie vadovu sandorius",
    "notifications on transactions concluded by managers",
    "vadovų sandori",
    "vadovu sandori",
)


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


def _manager_pdf_already_saved(pdf_url: str) -> bool:
    if not pdf_url:
        return False
    try:
        _headers, _url, _client = _supabase_client_parts()
        url = _url("manager_transactions")
        params = {"select": "pdf_url", "pdf_url": f"eq.{pdf_url}", "limit": "1"}
        with _client() as client:
            resp = client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            return bool(resp.json() or [])
    except Exception:
        return False


def _post_manager_transaction(row: dict) -> bool:
    _headers, _url, _client = _supabase_client_parts()
    url = _url("manager_transactions")
    with _client() as client:
        resp = client.post(
            url,
            headers={**_headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json=row,
        )
        if resp.status_code in (200, 201, 204):
            return True
        if resp.status_code == 409:
            return False
        raise RuntimeError(f"Supabase manager_transactions įrašymo klaida: {resp.status_code} - {resp.text}")


def _text_after_label(text: str, labels, max_len: int = 160) -> str:
    if not text:
        return ""
    for label in labels:
        pattern = rf"{label}\s*[:\-]?\s*(.+)"
        m = re.search(pattern, text, flags=re.I)
        if m:
            value = m.group(1).strip()
            value = re.split(r"\n|\r|\s{3,}", value)[0].strip()
            return value[:max_len]
    return ""


def _extract_number_after(text: str, labels) -> float | None:
    raw = _text_after_label(text, labels, max_len=80)
    if not raw:
        return None
    m = re.search(r"[-+]?\d[\d\s.,]*", raw)
    if not m:
        return None
    s = m.group(0).replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def _extract_date_from_text(text: str):
    if not text:
        return None
    patterns = [
        r"(\d{4}[-.]\d{2}[-.]\d{2})",
        r"(\d{2}[-.]\d{2}[-.]\d{4})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            dt = pd.to_datetime(m.group(1), errors="coerce", dayfirst=(pat.startswith('(\\d{2}')))
            if pd.notna(dt):
                return dt.date().isoformat()
    return None


def _extract_pdf_text(pdf_url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(pdf_url, headers=headers, timeout=45, verify=False)
    resp.raise_for_status()
    content = resp.content or b""
    if len(content) < 100:
        return ""
    text_parts = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for page in pdf.pages[:6]:
            try:
                text_parts.append(page.extract_text() or "")
            except Exception:
                pass
    return "\n".join(text_parts).strip()


def _extract_pdf_links_from_crib_page(driver, crib_url: str) -> list[str]:
    driver.get(crib_url)
    try:
        WebDriverWait(driver, 20).until(lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"})
    except Exception:
        pass

    links = []
    try:
        soup = BeautifulSoup(driver.page_source, "html.parser")
    except Exception:
        soup = None

    if soup is not None:
        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            txt = a.get_text(" ", strip=True).lower()
            if not href:
                continue
            full = urljoin(crib_url, href)
            low = full.lower()
            if ".pdf" in low or "download" in low or "attachment" in low or "getfile" in low or "file" in low or "pdf" in txt:
                links.append(full)

    # Shadow DOM / custom elements fallback.
    try:
        hrefs = driver.execute_script(
            """
            const out = [];
            function scan(root){
              root.querySelectorAll('a[href], nef-link[href]').forEach(a => out.push(a.href || a.getAttribute('href')));
              root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) scan(el.shadowRoot); });
            }
            scan(document);
            return out;
            """
        ) or []
        for href in hrefs:
            full = urljoin(crib_url, str(href or "").strip())
            low = full.lower()
            if full and (".pdf" in low or "download" in low or "attachment" in low or "getfile" in low):
                links.append(full)
    except Exception:
        pass

    clean = []
    seen = set()
    for u in links:
        if not u or u in seen:
            continue
        seen.add(u)
        clean.append(u)
    return clean


def _parse_manager_transaction_pdf_text(text: str, pdf_url: str, crib_url: str, published_at=None) -> dict:
    issuer = _text_after_label(text, [
        r"Emitento pavadinimas", r"Emitentas", r"Issuer name", r"Name of the issuer",
    ])
    person = _text_after_label(text, [
        r"Vadovo vardas ir pavardė", r"Asmens vardas ir pavardė", r"Name of the person", r"Person name",
    ])
    role = _text_after_label(text, [
        r"Pareigos", r"Statusas", r"Position", r"Function", r"Role",
    ])
    isin = _text_after_label(text, [r"ISIN", r"Finansinės priemonės identifikavimo kodas", r"Identification code"])
    instrument = _text_after_label(text, [
        r"Finansinės priemonės pavadinimas", r"Priemonės pavadinimas", r"Financial instrument", r"Instrument",
    ])
    transaction_type = _text_after_label(text, [
        r"Sandorio pobūdis", r"Sandorio rūšis", r"Nature of the transaction", r"Transaction type",
    ])
    venue = _text_after_label(text, [
        r"Sandorio vieta", r"Prekybos vieta", r"Place of the transaction", r"Trading venue", r"Venue",
    ])

    price = _extract_number_after(text, [r"Kaina", r"Price"])
    quantity = _extract_number_after(text, [r"Kiekis", r"Volume", r"Quantity"])
    transaction_date = None
    date_label_value = _text_after_label(text, [
        r"Sandorio data", r"Transaction date", r"Date of transaction",
    ], max_len=80)
    if date_label_value:
        transaction_date = _extract_date_from_text(date_label_value)
    if not transaction_date:
        transaction_date = _extract_date_from_text(text)

    published_iso = None
    try:
        if published_at is not None and not pd.isna(published_at):
            published_iso = pd.to_datetime(published_at).isoformat()
    except Exception:
        published_iso = None

    return {
        "published_at": published_iso,
        "transaction_date": transaction_date,
        "issuer": issuer,
        "person_name": person,
        "person_role": role,
        "isin": isin,
        "instrument": instrument,
        "transaction_type": transaction_type,
        "price": price,
        "quantity": quantity,
        "venue": venue,
        "parse_status": "parsed_basic" if text else "pdf_text_empty",
        "price_quantity_note": "",
        "pdf_url": pdf_url,
        "crib_url": crib_url,
        "raw_text": text[:12000] if text else "",
    }


def save_manager_transactions_from_crib_selenium(driver, crib_url: str, published_at=None) -> int:
    """
    Savarankiška versija be backfill_manager_transactions_from_crib.py.
    Iš CRIB pranešimo ištraukia PDF priedus, praleidžia jau DB esančius PDF,
    bando perskaityti PDF tekstą ir įrašo į manager_transactions.
    """
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
            row = _parse_manager_transaction_pdf_text(text, pdf_url=pdf_url, crib_url=crib_url, published_at=published_at)
            if _post_manager_transaction(row):
                saved += 1
        except Exception:
            # Įrašome bent PDF faktą, kad tas pats PDF nebūtų bandomas be galo.
            row = _parse_manager_transaction_pdf_text("", pdf_url=pdf_url, crib_url=crib_url, published_at=published_at)
            row["parse_status"] = "pdf_parse_error"
            try:
                if _post_manager_transaction(row):
                    saved += 1
            except Exception:
                pass
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


def update_manager_transactions_from_recent_crib(
    days_back: int = 45,
    max_messages: int = 30,
    headless: bool = True,
    progress=None,
) -> dict:
    """
    Patikrina paskutinius CRIB pranešimus market_news lentelėje ir papildo
    manager_transactions, jei vadovų sandorių PDF dar nėra įrašyti.
    """
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


def _load_manager_transactions_df_fallback(start_date, end_date) -> pd.DataFrame:
    """
    Atsarginis manager_transactions nuskaitymas tiesiai per Supabase REST.
    Reikalingas tada, kai supabase_cache.py dar neturi load_manager_transactions_df().
    """
    try:
        from supabase_cache import _supabase_headers, _supabase_rest_url, _http_client

        start_iso = f"{start_date}T00:00:00"
        end_iso = f"{end_date}T23:59:59"
        url = _supabase_rest_url("manager_transactions")
        params = {
            "select": "*",
            "published_at": [f"gte.{start_iso}", f"lte.{end_iso}"],
            "order": "published_at.desc",
        }

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


def load_crib_news_df(start_date, end_date) -> pd.DataFrame:
    df = load_news_df("crib", start_date, end_date)

    if df is None or df.empty:
        return pd.DataFrame(
            columns=[
                "issuer",
                "issuer_norm",
                "category",
                "title",
                "published_at",
                "crib_url",
                "content",
            ]
        )

    df = df.copy()

    df["issuer"] = df.get("company", "").fillna("").astype(str).str.strip()
    df["issuer_norm"] = df.get("company_norm", "").fillna("").astype(str).str.strip()
    df["category"] = df.get("category", "").fillna("").astype(str).str.strip()
    df["title"] = df.get("title", "").fillna("").astype(str).str.strip()
    df["published_at"] = df.get("published_at", None)
    df["crib_url"] = df.get("url", "").fillna("").astype(str).str.strip()
    df["content"] = df.get("content", "").fillna("").astype(str).str.strip()

    return df[
        [
            "issuer",
            "issuer_norm",
            "category",
            "title",
            "published_at",
            "crib_url",
            "content",
        ]
    ]


ANNUAL_PATTERNS = [
    r"\bmetin",
    r"\bannual\s+report\b",
    r"\baudited\b",
    r"\baudituot",
]

HALF_YEAR_PATTERNS = [
    r"\b6\s*m[ėe]n",
    r"\bšešių\s+m[ėe]nesių\b",
    r"\bpusme",
    r"\bhalf[- ]year\b",
    r"\bsemi[- ]annual\b",
    r"\b6\s*months\b",
    r"\bsix\s+months\b",
]

EXCLUDE_PATTERNS = [
    r"\b3\s*m[ėe]n",
    r"\b9\s*m[ėe]n",
    r"\bq1\b",
    r"\bq3\b",
    r"\bI\s+ketv",
    r"\bIII\s+ketv",
    r"\bketvir",
    r"\bpreliminar",
    r"\bprognoz",
    r"\bdividend",
]


def _norm_text(x) -> str:
    return str(x or "").lower().strip()


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def _classify_financial_report(row) -> str:
    category = _norm_text(row.get("category", ""))
    title = _norm_text(row.get("title", ""))
    content = _norm_text(row.get("content", ""))

    text = f"{category} {title} {content[:1000]}"

    if _matches_any(text, EXCLUDE_PATTERNS):
        return ""

    if "metin" in category and _matches_any(text, ANNUAL_PATTERNS):
        return "Metinė"

    if "tarpin" in category and _matches_any(text, HALF_YEAR_PATTERNS):
        return "Pusmečio / 6 mėn."

    return ""


def prepare_dpl_periods_df(news_df: pd.DataFrame) -> pd.DataFrame:
    if news_df is None or news_df.empty:
        return pd.DataFrame()

    df = news_df.copy()

    for col in ["issuer", "issuer_norm", "category", "title", "published_at", "crib_url", "content"]:
        if col not in df.columns:
            df[col] = ""

    df["report_published_date"] = pd.to_datetime(
        df["published_at"],
        errors="coerce",
        utc=True,
    ).dt.date

    for col in ["issuer", "issuer_norm", "category", "title", "crib_url", "content"]:
        df[col] = df[col].fillna("").astype(str).str.strip()

    df["dpl_report_type"] = df.apply(_classify_financial_report, axis=1)

    df = df[
        (df["issuer"] != "")
        & df["report_published_date"].notna()
        & (df["dpl_report_type"] != "")
    ].copy()

    if df.empty:
        return pd.DataFrame()

    df["dpl_start_date"] = df["report_published_date"].apply(
        lambda x: x - timedelta(days=30)
    )
    df["dpl_end_date"] = df["report_published_date"]

    return df[
        [
            "issuer",
            "issuer_norm",
            "dpl_report_type",
            "report_published_date",
            "dpl_start_date",
            "dpl_end_date",
            "title",
            "category",
            "crib_url",
        ]
    ].drop_duplicates()


def _issuer_key(value) -> str:
    return str(value or "").lower().strip()


def add_dpl_check_to_transactions(
    transactions_df: pd.DataFrame,
    dpl_periods_df: pd.DataFrame,
) -> pd.DataFrame:
    if transactions_df is None or transactions_df.empty:
        return pd.DataFrame()

    df = transactions_df.copy()

    df["is_dpl_period"] = False
    df["dpl_report_type"] = ""
    df["dpl_report_date"] = pd.NaT
    df["dpl_start_date"] = pd.NaT
    df["dpl_end_date"] = pd.NaT
    df["dpl_days_to_report"] = pd.NA
    df["dpl_report_title"] = ""
    df["dpl_report_url"] = ""

    if dpl_periods_df is not None and not dpl_periods_df.empty:
        periods = dpl_periods_df.copy()

        periods["issuer_key"] = periods["issuer"].apply(_issuer_key)
        df["issuer_key"] = df["issuer"].apply(_issuer_key)

        for idx, row in df.iterrows():
            issuer = row.get("issuer_key", "")
            trade_date = row.get("transaction_date_dt")

            if not issuer or pd.isna(trade_date):
                continue

            matches = periods[
                (periods["issuer_key"] == issuer)
                & (periods["dpl_start_date"] <= trade_date)
                & (periods["dpl_end_date"] >= trade_date)
            ].copy()

            if matches.empty:
                continue

            matches["days_to_report"] = matches["report_published_date"].apply(
                lambda x: (x - trade_date).days
            )

            match = matches.sort_values("days_to_report").iloc[0]

            df.at[idx, "is_dpl_period"] = True
            df.at[idx, "dpl_report_type"] = match["dpl_report_type"]
            df.at[idx, "dpl_report_date"] = match["report_published_date"]
            df.at[idx, "dpl_start_date"] = match["dpl_start_date"]
            df.at[idx, "dpl_end_date"] = match["dpl_end_date"]
            df.at[idx, "dpl_days_to_report"] = match["days_to_report"]
            df.at[idx, "dpl_report_title"] = match["title"]
            df.at[idx, "dpl_report_url"] = match["crib_url"]

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

    df = df.copy()

    for col in ["published_at", "transaction_date"]:
        if col not in df.columns:
            df[col] = None

    published_dt = pd.to_datetime(df["published_at"], errors="coerce", utc=True)
    transaction_dt = pd.to_datetime(df["transaction_date"], errors="coerce")

    df["published_date"] = published_dt.dt.date
    df["transaction_date_dt"] = transaction_dt.dt.date

    df["days_to_publish"] = (
        pd.to_datetime(df["published_date"], errors="coerce")
        - pd.to_datetime(df["transaction_date_dt"], errors="coerce")
    ).dt.days

    df["is_late_notification"] = df["days_to_publish"].apply(
        lambda x: bool(pd.notna(x) and x > 3)
    )

    for col in [
        "issuer",
        "person_name",
        "person_role",
        "isin",
        "instrument",
        "transaction_type",
        "venue",
        "parse_status",
        "price_quantity_note",
    ]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str).str.strip()

    for col in ["price", "quantity"]:
        if col not in df.columns:
            df[col] = None
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["transaction_value"] = df["price"] * df["quantity"]

    return df


def _apply_multiselect_filter(df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    if col not in df.columns:
        return df

    values = sorted([x for x in df[col].dropna().unique() if str(x).strip()])
    selected = st.multiselect(label, values, key=f"mgr_filter_{col}")

    if selected:
        return df[df[col].isin(selected)]

    return df


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


def _show_tables(df: pd.DataFrame):
    st.subheader("1. Detali vadovų sandorių lentelė")

    detail_cols = [
        "DPL",
        "DPL paaiškinimas",
        "DPL tipas",
        "DPL pradžia",
        "DPL pabaiga",
        "Ataskaitos paskelbimo data",
        "DPL dienų iki ataskaitos",
        "Susijusi ataskaita",
        "Ataskaitos nuoroda",
        "published_date",
        "transaction_date_dt",
        "days_to_publish",
        "is_late_notification",
        "issuer",
        "person_name",
        "person_role",
        "isin",
        "instrument",
        "transaction_type",
        "price",
        "quantity",
        "transaction_value",
        "price_quantity_note",
        "venue",
        "parse_status",
        "pdf_url",
        "crib_url",
    ]

    detail_cols = [c for c in detail_cols if c in df.columns]

    st.dataframe(df[detail_cols], use_container_width=True, hide_index=True)

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
        .sort_values(
            ["dpl_sandoriu_sk", "pranesimu_sk", "sandorio_bendra_verte"],
            ascending=[False, False, False],
        )
    )

    st.dataframe(person_summary, use_container_width=True, hide_index=True)


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

    with st.sidebar:
        st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-card-title">👔 Vadovų sandoriai</div>',
            unsafe_allow_html=True,
        )

        manager_start_date = st.date_input(
            "Pranešimo data nuo",
            value=date.today() - timedelta(days=30),
            key="manager_start_date",
        )

        manager_end_date = st.date_input(
            "Pranešimo data iki",
            value=date.today(),
            key="manager_end_date",
        )

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-card-subtitle">Atnaujina tik vadovų sandorių PDF iš paskutinių CRIB pranešimų, kurie jau yra market_news lentelėje.</div>',
            unsafe_allow_html=True,
        )

        manager_update_days = st.number_input(
            "Tikrinti paskutines dienas",
            min_value=7,
            max_value=180,
            value=45,
            step=1,
            key="manager_update_days",
        )

        manager_update_btn = st.button(
            "🔄 Atnaujinti vadovų sandorius",
            use_container_width=True,
            key="manager_transactions_update_btn",
        )

        st.markdown("</div>", unsafe_allow_html=True)

    if manager_update_btn:
        try:
            with st.spinner("Tikrinami paskutiniai CRIB vadovų sandorių pranešimai ir PDF..."):
                stats = update_manager_transactions_from_recent_crib(
                    days_back=int(manager_update_days),
                    max_messages=50,
                    headless=True,
                    progress=None,
                )

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

    if manager_start_date > manager_end_date:
        st.error("Data „nuo“ negali būti vėlesnė už datą „iki“.")
        st.stop()

    raw_df = load_manager_transactions_from_db(manager_start_date, manager_end_date)
    df = prepare_manager_transactions_df(raw_df)

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
            st.dataframe(
                dpl_periods_df[
                    [
                        "issuer",
                        "dpl_report_type",
                        "report_published_date",
                        "dpl_start_date",
                        "dpl_end_date",
                        "category",
                        "title",
                        "crib_url",
                    ]
                ],
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
            df = _apply_multiselect_filter(
                df,
                "person_name",
                "Vadovas / susijęs asmuo",
            )

        with c3:
            df = _apply_multiselect_filter(
                df,
                "transaction_type",
                "Sandorio pobūdis",
            )

        with c4:
            df = _apply_multiselect_filter(
                df,
                "parse_status",
                "Apdorojimo statusas",
            )

        delay_filter = st.selectbox(
            "Vėlavimo filtras",
            [
                "Visi",
                "Tik vėluojantys >3 d.",
                "Tik nevėluojantys <=3 d.",
                "Be apskaičiuoto termino",
            ],
            key="mgr_delay_filter",
        )

        if delay_filter == "Tik vėluojantys >3 d.":
            df = df[df["is_late_notification"] == True]
        elif delay_filter == "Tik nevėluojantys <=3 d.":
            df = df[
                (df["days_to_publish"].notna())
                & (df["days_to_publish"] <= 3)
            ]
        elif delay_filter == "Be apskaičiuoto termino":
            df = df[df["days_to_publish"].isna()]

        dpl_filter = st.selectbox(
            "DPL filtras",
            [
                "Visi",
                "Tik sandoriai per DPL",
                "Tik ne DPL",
            ],
            key="mgr_dpl_filter",
        )

        if dpl_filter == "Tik sandoriai per DPL":
            df = df[df["is_dpl_period"] == True]
        elif dpl_filter == "Tik ne DPL":
            df = df[df["is_dpl_period"] == False]

    _show_tables(df)

    st.download_button(
        "⬇ Atsisiųsti CSV",
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name="vadovu_sandoriai_su_dpl.csv",
        mime="text/csv",
        use_container_width=True,
    )

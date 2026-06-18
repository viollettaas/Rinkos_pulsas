# -*- coding: utf-8 -*-
"""
crib_update.py

Greitas CRIB (Nasdaq emitentu pranesimu) atnaujinimas i Supabase.

Naudojimo logika:
- atidaro https://www.crib.lt/;
- perjungia LT kalba;
- tikrina pirma CRIB puslapi nuo virsaus;
- jeigu URL jau yra Supabase market_news lenteleje -> STOP;
- jeigu URL naujas -> atidaro detalu puslapi, paima pilna teksta ir iraso i DB;
- jeigu yra keli nauji pranesimai is eiles, iraso juos visus iki pirmo jau DB esancio pranesimo;
- dublikatai papildomai ignoruojami per unique_key logika supabase_cache.py faile;
- jeigu pranesimas yra apie vadovu sandorius, papildomai apdoroja PDF, jei modulis yra prieinamas.
"""

import os
os.environ["WDM_SSL_VERIFY"] = "0"

import re
import time
import warnings
from urllib.parse import urljoin
from datetime import datetime, date

import pandas as pd
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=UserWarning)

from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException

from supabase_cache import save_news_df, load_news_df, log_scrape, _supabase_headers, _supabase_rest_url, _http_client

try:
    from backfill_manager_transactions_from_crib import save_manager_transactions_from_crib_selenium
except Exception:
    save_manager_transactions_from_crib_selenium = None

DETAIL_TIMEOUT = 18


def _notify(progress, message: str):
    if progress:
        progress(message)


def init_driver(headless: bool = True):
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
    options.binary_location = "/usr/bin/chromium"
    options.set_capability("acceptInsecureCerts", True)
    return webdriver.Chrome(options=options)


def wait_ready(driver, timeout=25):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"}
    )


def get_inner_text(driver, el):
    if el is None:
        return ""
    try:
        return (driver.execute_script("return arguments[0].innerText || arguments[0].textContent;", el) or "").strip()
    except Exception:
        try:
            return (el.text or "").strip()
        except Exception:
            return ""


_TZ_WITH_SPACE_RE = re.compile(r"\s+(EET|EEST|CET|CEST|UTC|GMT)\b", flags=re.IGNORECASE)


def parse_dt_safe(value: str):
    if not value or not isinstance(value, str):
        return None

    s = value.strip()
    s = _TZ_WITH_SPACE_RE.sub("", s)

    parts = s.rsplit(" ", 1)
    if len(parts) == 2 and re.fullmatch(r"[A-Za-z]{1,5}", parts[1]):
        s = parts[0]

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt)
        except Exception:
            pass

    try:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.notna(ts):
            return ts.to_pydatetime()
    except Exception:
        pass

    return None


def click_possible_cookie_banners(driver):
    candidates = [
        (By.CSS_SELECTOR, "button#onetrust-accept-btn-handler"),
        (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'accept')]"),
        (By.XPATH, "//button[contains(., 'Sutinku')]"),
        (By.XPATH, "//button[contains(., 'Leisti')]"),
        (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'allow')]"),
    ]
    for by, selector in candidates:
        try:
            el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, selector)))
            driver.execute_script("arguments[0].click();", el)
            time.sleep(0.4)
            return True
        except Exception:
            pass
    return False


def click_language_lt_real_button(driver, timeout=20) -> bool:
    try:
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR, "nef-navigation")))
        host = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'nef-navigation-button.language-selector[data-language="lt"]'))
        )
        try:
            shadow_root = host.shadow_root
        except Exception:
            shadow_root = driver.execute_script("return arguments[0].shadowRoot", host)

        button = WebDriverWait(driver, timeout).until(
            lambda d: shadow_root.find_element(By.CSS_SELECTOR, "button.nef-c-navigation-button__button")
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
        try:
            button.click()
        except Exception:
            driver.execute_script("arguments[0].click();", button)
        time.sleep(1.0)
        return True
    except Exception:
        return False


def extract_title_and_text_from_current_page(driver):
    try:
        wait_ready(driver, timeout=DETAIL_TIMEOUT)
    except Exception:
        pass
    time.sleep(0.3)

    try:
        soup = BeautifulSoup(driver.page_source, "html.parser")
    except Exception:
        return "", ""

    title = ""
    t_el = soup.find(["h1", "h2"])
    if t_el:
        title = t_el.get_text(" ", strip=True)

    selectors = [
        "article", "main", ".nef-message-details", ".notice", ".notice__content",
        ".page-content", ".content-area", ".content"
    ]

    best_text = ""
    for sel in selectors:
        for el in soup.select(sel):
            txt = el.get_text("\n", strip=True)
            if len(txt) > len(best_text):
                best_text = txt

    if not best_text:
        paras = soup.find_all("p")
        best_text = "\n\n".join(p.get_text(" ", strip=True) for p in paras[:12])

    best_text = re.sub(r"\r", "", best_text or "")
    best_text = re.sub(r"[ \t]+", " ", best_text)
    best_text = re.sub(r"\n{3,}", "\n\n", best_text).strip()

    return title.strip(), best_text


def open_detail_in_new_tab(driver, url: str):
    if not url:
        return "", ""

    main_handle = driver.current_window_handle
    try:
        driver.execute_script("window.open('about:blank','_blank');")
        driver.switch_to.window(driver.window_handles[-1])
        driver.get(url)
        title, text = extract_title_and_text_from_current_page(driver)
        driver.close()
        driver.switch_to.window(main_handle)
        return title, text
    except Exception:
        try:
            if len(driver.window_handles) > 1:
                driver.close()
            driver.switch_to.window(main_handle)
        except Exception:
            pass
        return "", ""


def find_row_link(driver, row):
    for sel in ["nef-link[href]", "a.table-link", "a[href]"]:
        try:
            link_el = row.find_element(By.CSS_SELECTOR, sel)
            href = link_el.get_attribute("href") or ""
            title = get_inner_text(driver, link_el)
            if href.startswith("/"):
                href = "https://www.crib.lt" + href
            if href and "lang=" in href:
                href = re.sub(r"(\?|&)lang=[a-z]{2}", r"\1lang=lt", href)
            elif href:
                sep = "&" if "?" in href else "?"
                href = f"{href}{sep}lang=lt"
            return href, title
        except Exception:
            pass
    return "", ""


def find_first_existing_text(driver, row, selectors):
    for sel in selectors:
        try:
            el = row.find_element(By.CSS_SELECTOR, sel)
            txt = get_inner_text(driver, el)
            if txt:
                return txt
        except Exception:
            pass
    return ""


def parse_crib_rows_on_page(driver):
    rows_out = []
    rows = driver.find_elements(By.CSS_SELECTOR, "nef-table-row.message-row")

    for row in rows:
        try:
            cells = row.find_elements(By.CSS_SELECTOR, "nef-table-cell")
            raw_date = ""
            if cells:
                raw_date = get_inner_text(driver, cells[0])

            if not raw_date:
                raw_date = find_first_existing_text(driver, row, [".table-date", "nef-table-cell.table-date"])

            dt = parse_dt_safe(raw_date)
            if dt is None:
                continue

            href, headline = find_row_link(driver, row)

            category = find_first_existing_text(driver, row, [
                ".table-category", "nef-table-cell.table-category"
            ])

            company = find_first_existing_text(driver, row, [
                ".table-issuer", ".table-company",
                "nef-table-cell.table-issuer", "nef-table-cell.table-company"
            ])

            rows_out.append({
                "Bendrovė": company,
                "Kategorija": category,
                "Naujiena": "",
                "Published_dt": dt,
                "Nuoroda": href,
                "Pilna_antraštė": headline,
                "Pilnas_tekstas": "",
            })
        except StaleElementReferenceException:
            continue
        except Exception:
            continue

    return rows_out


def add_company_norm(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    df = df.copy()
    df["Bendrovė_norm"] = (
        df["Bendrovė"].astype(str).str.lower()
        .str.replace(" group", "", regex=False)
        .str.replace(" grupė", "", regex=False)
        .str.replace(" bankas", "", regex=False)
        .str.replace(r"\b(uab|ab|as)\b", "", regex=True)
        .str.replace(r"[^\w\s]", "", regex=True)
        .str.strip()
    )
    return df


def make_news_label(row):
    dt = row.get("Published_dt")
    headline = str(row.get("Pilna_antraštė") or "").strip()
    url = str(row.get("Nuoroda") or "").strip()

    try:
        dt_label = pd.to_datetime(dt).strftime("%Y-%m-%d %H:%M")
    except Exception:
        dt_label = ""

    label = f"{dt_label} - {headline}" if headline else dt_label
    if url:
        return f'<a href="{url}" target="_blank">{label}</a>'
    return label


def is_manager_transactions_category(value: str) -> bool:
    text = str(value or "").lower()
    return (
        "pranešimai apie vadovų sandorius" in text
        or "pranesimai apie vadovu sandorius" in text
        or "notifications on transactions concluded by managers" in text
    )


def process_manager_transactions_for_records(driver, records, progress=None):
    stats = {
        "manager_messages_processed": 0,
        "manager_transactions_saved": 0,
        "manager_transactions_errors": 0,
    }

    if not records or save_manager_transactions_from_crib_selenium is None:
        return stats

    for r in records:
        category = str(r.get("Kategorija") or "")
        url = str(r.get("Nuoroda") or "").strip()
        published_at = r.get("Published_dt")

        if not url or not is_manager_transactions_category(category):
            continue

        stats["manager_messages_processed"] += 1
        try:
            saved = save_manager_transactions_from_crib_selenium(
                driver=driver,
                crib_url=url,
                published_at=published_at,
            )
            stats["manager_transactions_saved"] += int(saved or 0)
        except Exception:
            stats["manager_transactions_errors"] += 1

    return stats


def _normalize_url(url: str) -> str:
    url = str(url or "").strip().lower()
    url = re.sub(r"([?&])lang=[a-z]{2}", "", url)
    url = url.rstrip("?&")
    return url


def _make_existing_key_from_crib_row(row) -> str:
    url = _normalize_url(row.get("Nuoroda", ""))
    if url:
        return "url|" + url

    title = str(row.get("Pilna_antraštė", "") or "").strip().lower()
    published = row.get("Published_dt")
    try:
        published = pd.to_datetime(published, errors="coerce").strftime("%Y-%m-%d %H:%M")
    except Exception:
        published = str(published or "").strip()
    company = str(row.get("Bendrovė", "") or "").strip().lower()
    return f"fallback|{company}|{title}|{published}"


def load_recent_crib_keys(limit: int = 300) -> set:
    """
    Užkrauna tik naujausius CRIB URL/raktus iš DB.
    Nebekrauna visos CRIB istorijos, todėl atnaujinimas daug greitesnis.
    """
    url = _supabase_rest_url("market_news")
    params = {
        "select": "url,title,company,published_at",
        "source": "eq.crib",
        "order": "published_at.desc",
        "limit": str(limit),
    }

    try:
        with _http_client() as client:
            response = client.get(url, headers=_supabase_headers(), params=params)
            response.raise_for_status()
            data = response.json() or []
    except Exception:
        # Atsarginis variantas, jei tiesioginė REST užklausa nepavyktų.
        try:
            df_existing = load_news_df("crib", date.today().replace(year=max(2023, date.today().year - 1)), date.today())
            data = df_existing.to_dict("records") if df_existing is not None and not df_existing.empty else []
        except Exception:
            data = []

    keys = set()
    for row in data:
        db_url = _normalize_url(row.get("url", ""))
        if db_url:
            keys.add("url|" + db_url)
            continue

        title = str(row.get("title", "") or "").strip().lower()
        company = str(row.get("company", "") or "").strip().lower()
        published = row.get("published_at")
        try:
            published = pd.to_datetime(published, errors="coerce").strftime("%Y-%m-%d %H:%M")
        except Exception:
            published = str(published or "").strip()
        keys.add(f"fallback|{company}|{title}|{published}")

    return keys


def get_latest_crib_news_date():
    try:
        url = _supabase_rest_url("market_news")
        params = {
            "select": "published_at,title,company,url",
            "source": "eq.crib",
            "published_at": "not.is.null",
            "order": "published_at.desc",
            "limit": "1",
        }

        with _http_client() as client:
            response = client.get(url, headers=_supabase_headers(), params=params)
            response.raise_for_status()
            data = response.json() or []

        if not data:
            return None

        latest = pd.to_datetime(data[0].get("published_at"), errors="coerce")
        if pd.isna(latest):
            return None
        return latest.to_pydatetime()
    except Exception:
        try:
            df = load_news_df("crib", date(2023, 1, 1), date.today())
            if df is None or df.empty or "published_at" not in df.columns:
                return None
            latest = pd.to_datetime(df["published_at"], errors="coerce").max()
            if pd.isna(latest):
                return None
            return latest.to_pydatetime()
        except Exception:
            return None




def _requests_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.crib.lt/",
    }


def _clean_text(value: str) -> str:
    value = re.sub(r"\r", "", str(value or ""))
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def fetch_crib_first_page_html(timeout: int = 25) -> str:
    """
    Greitas CRIB pirmo puslapio gavimas per requests.
    Jei pavyksta, nereikia startuoti Selenium vien tam, kad nustatytume,
    jog naujų pranešimų nėra.
    """
    urls = [
        "https://www.crib.lt/?lang=lt",
        "https://www.crib.lt/",
    ]
    last_error = None
    for url in urls:
        try:
            response = requests.get(
                url,
                headers=_requests_headers(),
                timeout=timeout,
                verify=False,
            )
            response.raise_for_status()
            text = response.text or ""
            if "nef-table-row" in text or "message-row" in text or "Published" in text or "Company" in text:
                return text
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return ""


def _row_text_soup(el):
    if el is None:
        return ""
    return _clean_text(el.get_text(" ", strip=True))


def _extract_href_and_title_from_soup_row(row):
    link_el = row.select_one("nef-link[href], a.table-link[href], a[href]")
    href = ""
    title = ""
    if link_el is not None:
        href = (link_el.get("href") or "").strip()
        title = _row_text_soup(link_el)
    if href:
        href = urljoin("https://www.crib.lt/", href)
        if "lang=" in href:
            href = re.sub(r"([?&])lang=[a-z]{2}", r"\1lang=lt", href)
        else:
            sep = "&" if "?" in href else "?"
            href = f"{href}{sep}lang=lt"
    return href, title


def parse_crib_rows_from_html(html: str):
    """Parsina CRIB pirmo puslapio eilutes be Selenium."""
    soup = BeautifulSoup(html or "", "html.parser")
    rows = soup.select("nef-table-row.message-row")
    if not rows:
        # Atsarginis variantas, jei CRIB kada nors grąžintų įprastą lentelę.
        rows = soup.select("table tbody tr, table tr")

    out = []
    for row in rows:
        try:
            cells = row.select("nef-table-cell, td")
            if not cells:
                continue

            raw_date = _row_text_soup(cells[0]) if len(cells) >= 1 else ""
            dt = parse_dt_safe(raw_date)
            if dt is None:
                continue

            href, headline = _extract_href_and_title_from_soup_row(row)

            company = ""
            category = ""

            company_el = row.select_one(".table-issuer, .table-company, nef-table-cell.table-issuer, nef-table-cell.table-company")
            if company_el is not None:
                company = _row_text_soup(company_el)
            elif len(cells) >= 2:
                company = _row_text_soup(cells[1])

            category_el = row.select_one(".table-category, nef-table-cell.table-category")
            if category_el is not None:
                category = _row_text_soup(category_el)
            elif len(cells) >= 4:
                category = _row_text_soup(cells[-1])

            if not headline:
                # Dažniausiai: Published | Company | Headline | Message Category
                if len(cells) >= 3:
                    headline = _row_text_soup(cells[2])
                else:
                    headline = _row_text_soup(row)

            out.append({
                "Bendrovė": company,
                "Kategorija": category,
                "Naujiena": "",
                "Published_dt": dt,
                "Nuoroda": href,
                "Pilna_antraštė": headline,
                "Pilnas_tekstas": "",
            })
        except Exception:
            continue
    return out


def extract_title_and_text_from_html(html: str):
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return "", ""

    title = ""
    t_el = soup.find(["h1", "h2"])
    if t_el:
        title = t_el.get_text(" ", strip=True)

    selectors = [
        "article", "main", ".nef-message-details", ".notice", ".notice__content",
        ".page-content", ".content-area", ".content"
    ]
    best_text = ""
    for sel in selectors:
        for el in soup.select(sel):
            txt = el.get_text("\n", strip=True)
            if len(txt) > len(best_text):
                best_text = txt

    if not best_text:
        paras = soup.find_all("p")
        best_text = "\n\n".join(p.get_text(" ", strip=True) for p in paras[:12])

    return _clean_text(title), _clean_text(best_text)


def fetch_detail_fast(url: str, timeout: int = 25):
    if not url:
        return "", ""
    try:
        response = requests.get(
            url,
            headers=_requests_headers(),
            timeout=timeout,
            verify=False,
        )
        response.raise_for_status()
        return extract_title_and_text_from_html(response.text or "")
    except Exception:
        return "", ""


def start_driver_for_manager_transactions(headless: bool = True):
    """Selenium startuojamas tik tada, kai tikrai reikia PDF apdorojimui."""
    driver = init_driver(headless=headless)
    try:
        driver.get("https://www.crib.lt/")
        wait_ready(driver, timeout=30)
        click_possible_cookie_banners(driver)
        click_language_lt_real_button(driver, timeout=20)
    except Exception:
        pass
    return driver


def update_crib_news(max_pages: int = 1, headless: bool = True, progress=None, stop_empty_pages: int = None, recent_key_limit: int = 300):
    """
    Itin greitas CRIB atnaujinimas.

    Pagrindinė idėja:
    - pirmiausia per REST iš DB paimami tik paskutiniai recent_key_limit CRIB raktai;
    - CRIB pirmas puslapis paimamas per requests, be Selenium;
    - patikrinama pirma naujienos eilutė;
    - jei ji jau DB, iškart grąžinama, neatidarant detalių puslapių ir nestartuojant Selenium;
    - jei yra naujų eilučių virš pirmos DB esančios eilutės, tik joms imamas pilnas tekstas;
    - Selenium startuojamas tik tada, kai tarp naujų pranešimų yra vadovų sandorių PDF.
    """
    existing_keys = load_recent_crib_keys(limit=recent_key_limit)

    total_found = 0
    total_inserted = 0
    total_new_candidates = 0
    total_manager_messages_processed = 0
    total_manager_transactions_saved = 0
    total_manager_transactions_errors = 0
    pages_processed = 1

    try:
        _notify(progress, "Tikrinamas pirmas CRIB puslapis...")
        html = fetch_crib_first_page_html(timeout=25)
        rows_basic = parse_crib_rows_from_html(html)
        total_found = len(rows_basic)

        if not rows_basic:
            return {
                "pages_processed": pages_processed,
                "records_found": total_found,
                "records_inserted": 0,
                "new_candidates": 0,
                "manager_messages_processed": 0,
                "manager_transactions_saved": 0,
                "manager_transactions_errors": 0,
            }

        # Svarbiausias greičio patikrinimas: jei pati naujausia eilutė jau DB,
        # nieko daugiau nebedarome.
        first_key = _make_existing_key_from_crib_row(rows_basic[0])
        if existing_keys and first_key in existing_keys:
            return {
                "pages_processed": pages_processed,
                "records_found": total_found,
                "records_inserted": 0,
                "new_candidates": 0,
                "manager_messages_processed": 0,
                "manager_transactions_saved": 0,
                "manager_transactions_errors": 0,
            }

        page_dates = [r["Published_dt"].date() for r in rows_basic if r.get("Published_dt") is not None]
        newest_on_page = max(page_dates) if page_dates else date.today()
        oldest_on_page = min(page_dates) if page_dates else date.today()

        new_rows = []
        for r in rows_basic:
            key = _make_existing_key_from_crib_row(r)
            if existing_keys and key in existing_keys:
                break
            new_rows.append(r)

        if not new_rows:
            return {
                "pages_processed": pages_processed,
                "records_found": total_found,
                "records_inserted": 0,
                "new_candidates": 0,
                "manager_messages_processed": 0,
                "manager_transactions_saved": 0,
                "manager_transactions_errors": 0,
            }

        records = []
        for r in new_rows:
            url = str(r.get("Nuoroda") or "").strip()
            title = str(r.get("Pilna_antraštė") or "").strip()
            full_title, full_text = fetch_detail_fast(url) if url else ("", "")

            if full_title:
                r["Pilna_antraštė"] = full_title
            elif title:
                r["Pilna_antraštė"] = title

            r["Pilnas_tekstas"] = full_text or ""
            r["Naujiena"] = make_news_label(r)
            records.append(r)

        df_page = pd.DataFrame(records)
        df_page = add_company_norm(df_page)

        inserted = 0
        if df_page is not None and not df_page.empty:
            inserted = save_news_df(df_page, "crib")
            try:
                log_scrape("crib", newest_on_page, oldest_on_page, "success", len(df_page))
            except Exception:
                pass

        total_new_candidates = len(df_page) if df_page is not None else 0
        total_inserted = int(inserted or 0)

        manager_records = [r for r in records if is_manager_transactions_category(r.get("Kategorija"))]
        if manager_records and save_manager_transactions_from_crib_selenium is not None:
            driver = None
            try:
                driver = start_driver_for_manager_transactions(headless=headless)
                manager_stats = process_manager_transactions_for_records(
                    driver=driver,
                    records=manager_records,
                    progress=progress,
                )
                total_manager_messages_processed = manager_stats.get("manager_messages_processed", 0)
                total_manager_transactions_saved = manager_stats.get("manager_transactions_saved", 0)
                total_manager_transactions_errors = manager_stats.get("manager_transactions_errors", 0)
            finally:
                try:
                    if driver is not None:
                        driver.quit()
                except Exception:
                    pass

        return {
            "pages_processed": pages_processed,
            "records_found": total_found,
            "records_inserted": total_inserted,
            "new_candidates": total_new_candidates,
            "manager_messages_processed": total_manager_messages_processed,
            "manager_transactions_saved": total_manager_transactions_saved,
            "manager_transactions_errors": total_manager_transactions_errors,
        }

    except Exception as e:
        try:
            log_scrape("crib", date.today(), date.today(), "error", 0, str(e))
        except Exception:
            pass
        raise


# -*- coding: utf-8 -*-
"""
crib_update.py

Greitas CRIB (Nasdaq emitentu pranesimu) atnaujinimas i Supabase.

Skirta naudoti is Streamlit mygtuko:
    from crib_update import update_crib_news
    stats = update_crib_news()

Logika:
- atidaro https://www.crib.lt/;
- perjungia LT kalba;
- eina nuo naujausiu CRIB puslapiu;
- nuskaito pranesimus, atidaro detalius puslapius ir paima pilna teksta;
- saugo i ta pacia Supabase lentele market_news per save_news_df(..., "crib");
- jei pranesimo kategorija yra "Pranesimai apie vadovu sandorius", papildomai nuskaito PDF priedus ir saugo i manager_transactions;
- dublikatai ignoruojami per unique_key logika supabase_cache.py ir pdf_url manager_transactions lenteleje;
- jei pirmame puslapyje nieko naujo neranda, sustoja po pirmo puslapio;
- jei randa nauju pranesimu, eina iki pirmo jau DB esancio pranesimo.

Svarbus pataisymas Streamlit Cloud:
- nebenaudojamas webdriver_manager ChromeDriverManager(), nes Streamlit Cloud aplinkoje jis gali parinkti
  ChromeDriver versija, nesuderinama su /usr/bin/chromium;
- naudojamas Selenium Manager per webdriver.Chrome(options=options), kuris parenka suderinama driveri.
"""

import os
os.environ["WDM_SSL_VERIFY"] = "0"

import re
import time
import warnings
from datetime import datetime, date

import pandas as pd
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

from supabase_cache import save_news_df, load_news_df, log_scrape

try:
    from backfill_manager_transactions_from_crib import save_manager_transactions_from_crib_selenium
except Exception:
    save_manager_transactions_from_crib_selenium = None


NEXT_BUTTON_HOST_ID = "pagination"
DETAIL_TIMEOUT = 18
PAGE_SLEEP = 0.8


def _notify(progress, message: str):
    if progress:
        progress(message)


def init_driver(headless: bool = True):
    """
    Sukuria Chrome/Chromium driveri.

    Naudojamas Selenium Manager, o ne webdriver_manager.ChromeDriverManager(),
    kad Streamlit Cloud aplinkoje neatsirastu klaida:
        ChromeDriver only supports Chrome version X, current browser version Y.
    """
    options = Options()
    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--window-size=1600,1200")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--lang=lt")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors=yes")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.set_capability("acceptInsecureCerts", True)

    chromium_paths = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    for path in chromium_paths:
        if os.path.exists(path):
            options.binary_location = path
            break

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

    if not records:
        return stats

    if save_manager_transactions_from_crib_selenium is None:
        _notify(progress, "Vadovu sandoriu PDF modulis nerastas, todel manager_transactions neatnaujinta.")
        return stats

    for r in records:
        category = str(r.get("Kategorija") or "")
        url = str(r.get("Nuoroda") or "").strip()
        published_at = r.get("Published_dt")

        if not url:
            continue

        if not is_manager_transactions_category(category):
            continue

        stats["manager_messages_processed"] += 1
        _notify(progress, f"Nuskaitomi vadovu sandoriu PDF: {url}")

        try:
            saved = save_manager_transactions_from_crib_selenium(
                driver=driver,
                crib_url=url,
                published_at=published_at,
            )
            stats["manager_transactions_saved"] += int(saved or 0)
        except Exception as exc:
            stats["manager_transactions_errors"] += 1
            _notify(progress, f"Vadovu sandoriu PDF klaida: {exc}")

    return stats


def click_next_in_shadow_pagination(driver, host_id=NEXT_BUTTON_HOST_ID):
    js = f"""
    try {{
        const host = document.getElementById('{host_id}');
        if (!host) return {{ok:false, reason:'no_host'}};
        const sr = host.shadowRoot;
        if (!sr) return {{ok:false, reason:'no_shadow'}};

        let btn = sr.querySelector('button[aria-label="Next page"], button[aria-label*="Next"], button[title*="Next"]');

        if (!btn) {{
            const icons = sr.querySelectorAll('i, nef-icon');
            for (const ic of icons) {{
                const txt = (ic.innerText || ic.textContent || '').toLowerCase();
                if (txt.includes('chevron_right') || txt.includes('chevron right')) {{
                    btn = ic.closest ? ic.closest('button') : null;
                    if (btn) break;
                }}
            }}
        }}

        if (!btn) {{
            const buttons = Array.from(sr.querySelectorAll('button'));
            for (const b of buttons) {{
                const txt = (b.innerText || b.textContent || '').toLowerCase();
                const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                if (txt.includes('next') || aria.includes('next') || txt.includes('>') || txt.includes('›')) {{
                    btn = b;
                    break;
                }}
            }}
        }}

        if (!btn) return {{ok:false, reason:'no_button'}};
        if (btn.disabled || btn.getAttribute('disabled') !== null) return {{ok:false, reason:'disabled'}};

        btn.scrollIntoView({{block:'center'}});
        try {{ btn.click(); }}
        catch(e) {{ btn.dispatchEvent(new MouseEvent('click', {{bubbles:true, cancelable:true}})); }}
        return {{ok:true, reason:'clicked'}};
    }} catch(e) {{
        return {{ok:false, reason:'exception', err:String(e)}};
    }}
    """
    try:
        res = driver.execute_script(js) or {}
        return bool(res.get("ok")), res.get("reason", "")
    except Exception as e:
        return False, str(e)


def first_row_signature(driver):
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "nef-table-row.message-row")
        if not rows:
            return ""
        txt = get_inner_text(driver, rows[0])
        return re.sub(r"\s+", " ", txt).strip()
    except Exception:
        return ""


def wait_for_page_change(driver, old_signature, timeout=12):
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(0.6)
        sig = first_row_signature(driver)
        if sig and sig != old_signature:
            return True
    return False


def _make_existing_key_from_db_row(row) -> str:
    url = str(row.get("url", "") or row.get("Nuoroda", "") or "").strip().lower()
    if url:
        return "url|" + url

    title = str(row.get("title", "") or row.get("Pilna_antraštė", "") or "").strip().lower()
    published = str(row.get("published_at", "") or row.get("Published_dt", "") or "").strip()
    try:
        published = pd.to_datetime(published, errors="coerce").strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    company = str(row.get("company", "") or row.get("Bendrovė", "") or "").strip().lower()
    return f"fallback|{company}|{title}|{published}"


def _make_existing_key_from_crib_row(row) -> str:
    url = str(row.get("Nuoroda", "") or "").strip().lower()
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


def load_existing_crib_keys(start_lookup: date = date(2023, 1, 1)) -> set:
    try:
        df_existing = load_news_df("crib", start_lookup, date.today())
    except Exception:
        return set()

    if df_existing is None or df_existing.empty:
        return set()

    return {
        _make_existing_key_from_db_row(row)
        for _, row in df_existing.iterrows()
        if _make_existing_key_from_db_row(row)
    }


def get_latest_crib_news_date():
    try:
        from supabase_cache import _supabase_headers, _supabase_rest_url, _http_client

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


def update_crib_news(max_pages: int = 20, headless: bool = True, progress=None, stop_empty_pages: int = None):
    existing_keys = load_existing_crib_keys()

    driver = init_driver(headless=headless)
    total_found = 0
    total_inserted = 0
    total_new_candidates = 0
    total_manager_messages_processed = 0
    total_manager_transactions_saved = 0
    total_manager_transactions_errors = 0
    page = 1
    reached_existing = False

    try:
        _notify(progress, "Atidaromas CRIB...")
        driver.get("https://www.crib.lt/")
        wait_ready(driver, timeout=30)
        time.sleep(1.0)
        click_possible_cookie_banners(driver)

        _notify(progress, "Perjungiama LT kalba...")
        click_language_lt_real_button(driver, timeout=20)

        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "nef-table-row.message-row"))
        )

        while page <= max_pages:
            _notify(progress, f"Tikrinamas CRIB puslapis {page}...")
            rows_basic = parse_crib_rows_on_page(driver)

            if not rows_basic:
                break

            page_dates = [r["Published_dt"].date() for r in rows_basic if r.get("Published_dt") is not None]
            newest_on_page = max(page_dates) if page_dates else date.today()
            oldest_on_page = min(page_dates) if page_dates else date.today()

            new_rows = []
            for r in rows_basic:
                key = _make_existing_key_from_crib_row(r)

                if existing_keys and key in existing_keys:
                    reached_existing = True
                    break

                new_rows.append(r)

            if page == 1 and not new_rows:
                _notify(progress, "Nauju CRIB pranesimu pirmame puslapyje nerasta.")
                total_found += len(rows_basic)
                break

            records = []
            for idx, r in enumerate(new_rows, start=1):
                url = str(r.get("Nuoroda") or "").strip()
                title = str(r.get("Pilna_antraštė") or "").strip()
                full_title, full_text = "", ""

                if url:
                    full_title, full_text = open_detail_in_new_tab(driver, url)

                if full_title:
                    r["Pilna_antraštė"] = full_title
                elif title:
                    r["Pilna_antraštė"] = title

                r["Pilnas_tekstas"] = full_text or ""
                r["Naujiena"] = make_news_label(r)
                records.append(r)

            df_page = pd.DataFrame(records)
            df_page = add_company_norm(df_page)

            found = len(df_page)
            inserted = 0
            manager_stats = {
                "manager_messages_processed": 0,
                "manager_transactions_saved": 0,
                "manager_transactions_errors": 0,
            }

            if found > 0:
                inserted = save_news_df(df_page, "crib")
                try:
                    log_scrape("crib", newest_on_page, oldest_on_page, "success", found)
                except Exception:
                    pass

                for _, row in df_page.iterrows():
                    existing_keys.add(_make_existing_key_from_crib_row(row))

                manager_stats = process_manager_transactions_for_records(
                    driver=driver,
                    records=records,
                    progress=progress,
                )

            total_found += len(rows_basic)
            total_new_candidates += found
            total_inserted += inserted
            total_manager_messages_processed += manager_stats.get("manager_messages_processed", 0)
            total_manager_transactions_saved += manager_stats.get("manager_transactions_saved", 0)
            total_manager_transactions_errors += manager_stats.get("manager_transactions_errors", 0)

            if reached_existing:
                break

            old_sig = first_row_signature(driver)
            clicked, _reason = click_next_in_shadow_pagination(driver)
            if not clicked:
                break

            changed = wait_for_page_change(driver, old_sig, timeout=12)
            if not changed:
                break

            page += 1
            time.sleep(PAGE_SLEEP)

        return {
            "pages_processed": page,
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

    finally:
        try:
            driver.quit()
        except Exception:
            pass

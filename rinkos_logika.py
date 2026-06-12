# -*- coding: utf-8 -*-
"""
rinkos_logika.py

Visa rinkos ataskaitos logika, pritaikyta naudoti su Streamlit.
Šis failas neturi Streamlit UI; jį importuoja app.py.
"""

import os
os.environ["WDM_SSL_VERIFY"] = "0"

import re
import time
import hashlib
import warnings
from difflib import SequenceMatcher
from datetime import datetime, date, timedelta
from io import BytesIO
from supabase_cache import save_news_df, load_news_df, log_scrape, _supabase_headers, _supabase_rest_url, _http_client
import requests
import httpx
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl.styles.stylesheet")

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


NASDAQ_NEWS_URL = "https://nasdaqbaltic.com/statistics/lt/news"

SEGMENTAI = {
    "Akcijos": ["Baltijos Papildomasis sąrašas", "Baltijos Oficialusis sąrašas"],
    "Obligacijos": ["Baltijos skolos VP sąrašas"],
}

IZVALGU_SPALVOS = {
    "🔥 Staigus aktyvumas su didele apimtimi": "#ff6f61",
    "🚨 Didelis poveikis rinkai": "#ff9999",
    "⚠️ Mažas likvidumas + kainos pokytis": "#ffd966",
    "🔍 Didelė apyvarta be kainos pokyčio": "#b4c6e7",
    "🔁 Aktyvumas be kainos pokyčio": "#a9d18e",
}


def extract_dates_from_filename(filename: str):
    m = re.search(r"(\d{8})_(\d{8})", filename or "")
    if not m:
        return None, None
    start_date = datetime.strptime(m.group(1), "%Y%m%d").date()
    end_date = datetime.strptime(m.group(2), "%Y%m%d").date()
    return start_date, end_date


def report_caption_from_filename(filename: str) -> str:
    start_date, end_date = extract_dates_from_filename(filename)
    if start_date and end_date:
        return f"Rinkos apžvalga ({start_date} – {end_date})"
    return "Rinkos apžvalga"


def normalize(text: str) -> str:
    if not isinstance(text, str):
        return ""
    text = text.lower()
    text = re.sub(r"(group|grupė|bankas)", "", text)
    text = re.sub(r"\b(uab|ab|as)\b", "", text)
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def atitinka(pavadinimas: str, antraste: str) -> bool:
    """
    SPECIALUS ATVEJIS AUGA GROUP:
    Jei įmonės pavadinime yra 'auga' ir 'group', atitinka tik jei antraštėje yra 'auga group'.
    """
    if not isinstance(pavadinimas, str) or not isinstance(antraste, str):
        return False

    if re.search(r"\bauga\b", pavadinimas, flags=re.I) and re.search(r"\bgroup\b", pavadinimas, flags=re.I):
        return bool(re.search(r"\bauga\s+group\b", antraste, flags=re.I))

    pavad = normalize(pavadinimas)
    antr = normalize(antraste)
    if not pavad or not antr:
        return False
    if pavad in antr:
        return True
    return SequenceMatcher(None, pavad, antr).ratio() > 0.65


def extract_date_from_url(url: str):
    match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url or "")
    if match:
        return datetime.strptime("-".join(match.groups()), "%Y-%m-%d").date()
    return None


def get_inner_text(driver, el):
    try:
        return (driver.execute_script("return arguments[0].innerText || arguments[0].textContent;", el) or "").strip()
    except Exception:
        try:
            return (el.text or "").strip()
        except Exception:
            return ""


def extract_title_and_text_generic(driver, timeout=15):
    """Paimame H1/H2 + pagrindinį tekstą (main/article); jei nepavyksta – didžiausią tekstinį bloką."""
    title, content = "", ""
    try:
        WebDriverWait(driver, timeout).until(lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:
        pass

    try:
        for sel in ["h1", "header h1", "article h1", "h2", ".title", ".headline"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                title = get_inner_text(driver, els[0])
                if title:
                    break
    except Exception:
        pass

    candidates = [
        "article", "main", "section.article", "div.article", "div.article-body",
        "div.article-content", ".content", ".vz-article__content", ".vz-article__body",
        ".nef-message-details", ".notice", ".notice__content", ".page-content", ".content-area",
    ]
    try:
        for sel in candidates:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            best = ""
            for e in els:
                t = get_inner_text(driver, e)
                if len(t) > len(best):
                    best = t
            if len(best) > 100:
                content = best
                break
    except Exception:
        pass

    if not content:
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            content = get_inner_text(driver, body)
        except Exception:
            content = ""

    def clean_text(t):
        t = re.sub(r"\r", "", t or "")
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()

    return clean_text(title), clean_text(content)


def _init_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1600,1200")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)


def _open_new_tab_and_get(driver, url, timeout=25):
    """Atidarome naują skirtuką, paimame (title, content), grįžtame į pagrindinį tabą."""
    try:
        driver.execute_script("window.open('about:blank','_blank');")
        driver.switch_to.window(driver.window_handles[-1])
        driver.get(url)
        t, c = extract_title_and_text_generic(driver, timeout=timeout)
        driver.close()
        driver.switch_to.window(driver.window_handles[0])
        return t, c
    except Exception:
        try:
            driver.get(url)
            t, c = extract_title_and_text_generic(driver, timeout=timeout)
        except Exception:
            t, c = "", ""
        return t, c


def _click_possible_cookie_banners(driver):
    candidates = [
        (By.CSS_SELECTOR, "button#onetrust-accept-btn-handler"),
        (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'accept')]"),
        (By.XPATH, "//button[contains(., 'Sutinku')]"),
        (By.XPATH, "//button[contains(., 'Leisti')]"),
        (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'allow')]"),
    ]
    for by, sel in candidates:
        try:
            el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, sel)))
            el.click()
            time.sleep(0.5)
        except Exception:
            pass


def _wait_ready(driver, timeout=30):
    WebDriverWait(driver, timeout).until(lambda d: d.execute_script("return document.readyState") == "complete")


def _click_language_lt_real_button(driver, timeout=20) -> bool:
    WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR, "nef-navigation")))
    host = WebDriverWait(driver, timeout).until(EC.presence_of_element_located(
        (By.CSS_SELECTOR, 'nef-navigation-button.language-selector[data-language="lt"]')
    ))
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
    return True


def scrape_crib_dom_lt(start_date: date, end_date: date, progress=None) -> pd.DataFrame:
    if progress:
        progress("CRIB naujienos: prisijungiama prie CRIB.lt...")
    driver = _init_driver()
    records = []
    try:
        driver.get("https://www.crib.lt/")
        _wait_ready(driver, 35)
        _click_possible_cookie_banners(driver)

        if progress:
            progress("CRIB: perjungiama LT kalba...")
        _click_language_lt_real_button(driver, timeout=20)

        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "nef-table-row.message-row"))
        )

        for _ in range(4):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.8)

        rows = driver.find_elements(By.CSS_SELECTOR, "nef-table-row.message-row")
        if progress:
            progress(f"CRIB: rasta {len(rows)} naujienų eilučių")

        for row in rows:
            try:
                link_el = None
                for sel in ["nef-link[href]", "a.table-link"]:
                    try:
                        link_el = row.find_element(By.CSS_SELECTOR, sel)
                        break
                    except Exception:
                        pass

                headline = ""
                href = None
                if link_el:
                    headline = get_inner_text(driver, link_el)
                    try:
                        href = link_el.get_attribute("href")
                    except Exception:
                        href = None

                cat_el = None
                for sel in [".table-category", "nef-table-cell.table-category"]:
                    try:
                        cat_el = row.find_element(By.CSS_SELECTOR, sel)
                        break
                    except Exception:
                        pass
                category = get_inner_text(driver, cat_el) if cat_el else ""

                cmp_el = None
                for sel in [".table-issuer", ".table-company", "nef-table-cell.table-issuer", "nef-table-cell.table-company"]:
                    try:
                        cmp_el = row.find_element(By.CSS_SELECTOR, sel)
                        break
                    except Exception:
                        pass
                company = get_inner_text(driver, cmp_el) if cmp_el else ""

                dt_el = None
                for sel in [".table-date", "nef-table-cell.table-date"]:
                    try:
                        dt_el = row.find_element(By.CSS_SELECTOR, sel)
                        break
                    except Exception:
                        pass
                if dt_el is None:
                    try:
                        dt_el = row.find_elements(By.CSS_SELECTOR, "nef-table-cell")[0]
                    except Exception:
                        pass

                published_raw = get_inner_text(driver, dt_el) if dt_el else ""
                published_txt = re.sub(r"\s+[A-Za-z]{2,5}$", "", published_raw).strip()

                dt = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(published_txt, fmt)
                        break
                    except ValueError:
                        continue
                if dt is None:
                    try:
                        dt = pd.to_datetime(published_raw).to_pydatetime()
                    except Exception:
                        continue

                if not (start_date <= dt.date() <= end_date):
                    continue

                if href and href.startswith("/"):
                    href = "https://www.crib.lt" + href

                if href:
                    if "lang=" in href:
                        href = re.sub(r"(\?|&)lang=[a-z]{2}", r"\1lang=lt", href)
                    else:
                        sep = "&" if "?" in href else "?"
                        href = f"{href}{sep}lang=lt"

                label = f"{dt.strftime('%Y-%m-%d %H:%M')} – {headline}" if headline else dt.strftime("%Y-%m-%d %H:%M")
                if href:
                    label = f'<a href="{href}" target="_blank">{label}</a>'

                full_title, full_text = ("", "")
                if href:
                    full_title, full_text = _open_new_tab_and_get(driver, href, timeout=20)

                records.append({
                    "Bendrovė": company,
                    "Kategorija": category,
                    "Naujiena": label,
                    "Published_dt": dt,
                    "Nuoroda": href or "",
                    "Pilna_antraštė": full_title or headline or "",
                    "Pilnas_tekstas": full_text or "",
                })
            except Exception:
                continue
    finally:
        driver.quit()

    df_news = pd.DataFrame(records)
    if df_news.empty:
        return pd.DataFrame(columns=[
            "Bendrovė", "Kategorija", "Naujiena", "Published_dt", "Nuoroda",
            "Pilna_antraštė", "Pilnas_tekstas", "Bendrovė_norm",
        ])

    df_news["Bendrovė_norm"] = (
        df_news["Bendrovė"].astype(str).str.lower()
        .str.replace(" group", "", regex=False)
        .str.replace(" grupė", "", regex=False)
        .str.replace(" bankas", "", regex=False)
        .str.replace(r"\b(uab|ab|as)\b", "", regex=True)
        .str.replace(r"[^\w\s]", "", regex=True)
        .str.strip()
    )
    df_news = df_news.sort_values("Published_dt", ascending=False).reset_index(drop=True)
    return df_news



def _clean_article_text(text: str) -> str:
    text = re.sub(r"\r", "", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_vz_article_fast(url: str):
    """
    Greitas VZ straipsnio pilno teksto paėmimas per requests.
    Selenium naudojamas tik VZ sąrašui. Tai daug greičiau nei kiekvieną straipsnį
    atidarinėti naujame Selenium tab'e.
    """
    if not url:
        return "", ""

    try:
        response = requests.get(
            url,
            timeout=15,
            verify=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
            },
        )
        response.raise_for_status()
    except Exception:
        return "", ""

    soup = BeautifulSoup(response.text or "", "html.parser")

    for bad in soup.select("script, style, noscript, iframe, svg, form, nav, footer, header"):
        bad.decompose()

    title = ""
    for sel in [
        "h1",
        "article h1",
        "meta[property='og:title']",
        "meta[name='twitter:title']",
        ".vz-article__title",
        ".article-title",
    ]:
        el = soup.select_one(sel)
        if not el:
            continue
        if el.name == "meta":
            title = (el.get("content") or "").strip()
        else:
            title = el.get_text(" ", strip=True)
        if title:
            break

    selectors = [
        "article",
        "main article",
        ".vz-article__content",
        ".vz-article__body",
        ".article-content",
        ".article-body",
        ".content",
        "main",
    ]

    best_text = ""
    for sel in selectors:
        for el in soup.select(sel):
            txt = el.get_text("\n", strip=True)
            txt = _clean_article_text(txt)
            if len(txt) > len(best_text):
                best_text = txt

    if not best_text:
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        best_text = _clean_article_text("\n\n".join([x for x in paragraphs if x]))

    return title.strip(), best_text


def _company_title_candidates(df_stat: pd.DataFrame) -> list:
    if df_stat is None or df_stat.empty or "Bendrovė" not in df_stat.columns:
        return []
    return [str(x).strip() for x in df_stat["Bendrovė"].dropna().unique() if str(x).strip()]


def _vz_title_matches_needed_companies(title: str, companies: list) -> bool:
    if not companies:
        return True
    return any(atitinka(company, title) for company in companies)


def vz_scrape_full(start_date, end_date, df_stat: pd.DataFrame, progress=None):
    """
    VŽ: sąrašas + pilni tekstai tik reikalingoms įmonėms.

    Greitinimas:
    - Selenium naudojamas tik VŽ pradžios / sąrašo puslapiui.
    - Detalūs straipsniai atsiunčiami per requests paraleliai.
    - Pilnas tekstas traukiamas tik tiems straipsniams, kurių antraštė atitinka
      bent vieną bendrovę iš statistikos failo.
    - Rezultatas išlieka suderinamas su visa ataskaita: vz_df turi
      Antraštė, Nuoroda, Data, Pilnas_tekstas, todėl pilni tekstai patenka į
      „Visos naujienos“ ir HTML ataskaitą.
    """
    if progress:
        progress("VŽ: renkami straipsniai pagal reikalingas bendroves...")

    companies = _company_title_candidates(df_stat)

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1600,1200")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    list_items = []

    try:
        driver.get("https://www.vz.lt/")
        time.sleep(1)

        try:
            accept_button = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.ID, "CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"))
            )
            accept_button.click()
            time.sleep(0.5)
        except Exception:
            pass

        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "article.vz-article"))
            )
        except Exception:
            pass

        articles = driver.find_elements(By.CSS_SELECTOR, "article.vz-article")
        if progress:
            progress(f"VŽ: sąraše rasta straipsnių: {len(articles)}")

        seen_urls = set()
        for el in articles:
            try:
                # Senas selektorius paliekamas, bet pridedame atsarginius variantus.
                link_el = None
                for selector in [
                    "div.vz-article__summary--description a",
                    ".vz-article__summary a",
                    "a[href]",
                ]:
                    try:
                        link_el = el.find_element(By.CSS_SELECTOR, selector)
                        if link_el:
                            break
                    except Exception:
                        pass

                if not link_el:
                    continue

                nuoroda = link_el.get_attribute("href") or ""
                antraste = (link_el.text or "").strip()

                if not antraste:
                    try:
                        antraste = el.text.strip().split("\n")[0]
                    except Exception:
                        antraste = ""

                if not nuoroda or nuoroda in seen_urls:
                    continue

                data_url = extract_date_from_url(nuoroda)
                if data_url is None or not (start_date <= data_url <= end_date):
                    continue

                # Svarbiausias greitinimas: pilną tekstą imame tik jei antraštė susijusi su įmonėmis.
                if not _vz_title_matches_needed_companies(antraste, companies):
                    continue

                seen_urls.add(nuoroda)
                list_items.append({
                    "Antraštė": antraste,
                    "Nuoroda": nuoroda,
                    "Data": data_url,
                })
            except Exception:
                continue
    finally:
        driver.quit()

    if not list_items:
        return {}, pd.DataFrame(columns=["Antraštė", "Nuoroda", "Data", "Pilnas_tekstas"])

    if progress:
        progress(f"VŽ: atrinkta susijusių straipsnių: {len(list_items)}. Traukiami pilni tekstai...")

    # Detalius straipsnius traukiame paraleliai per requests.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    by_url = {item["Nuoroda"]: item for item in list_items}
    fetched = {}

    max_workers = min(6, max(1, len(by_url)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(fetch_vz_article_fast, url): url
            for url in by_url.keys()
        }
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                fetched[url] = future.result()
            except Exception:
                fetched[url] = ("", "")

    items = []
    for item in list_items:
        url = item["Nuoroda"]
        title_full, content = fetched.get(url, ("", ""))
        title = title_full.strip() if title_full else item["Antraštė"]

        items.append({
            "Antraštė": title.strip(),
            "Nuoroda": url,
            "Data": item["Data"],
            "Pilnas_tekstas": (content or "").strip(),
        })

    vz_df = pd.DataFrame(items)

    vz_map = {}
    for imone in companies:
        found = ""
        for _, row in vz_df.iterrows():
            if atitinka(imone, row.get("Antraštė", "")):
                found = f'<a href="{row["Nuoroda"]}" target="_blank">{row["Antraštė"]}</a>'
                break
        vz_map[imone] = found

    return vz_map, vz_df

def _safe_click(driver, by, selector, timeout=10):
    el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((by, selector)))
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)
    return el


def _try_accept_cookies(driver):
    candidates = [
        (By.ID, "CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"),
        (By.CSS_SELECTOR, "button#onetrust-accept-btn-handler"),
        (By.XPATH, "//button[contains(., 'Sutinku')]"),
        (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'accept')]"),
        (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'allow')]"),
    ]
    for by, sel in candidates:
        try:
            _safe_click(driver, by, sel, timeout=3)
            time.sleep(0.6)
            return
        except Exception:
            pass


def _set_date_input(driver, label_text: str, value_yyyy_mm_dd: str):
    xpaths = [
        f"//label[contains(normalize-space(.), '{label_text}')]/following::input[1]",
        f"//*[contains(normalize-space(.), '{label_text}')]/following::input[1]",
    ]
    for xp in xpaths:
        try:
            inp = WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.XPATH, xp)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)
            inp.clear()
            inp.send_keys(value_yyyy_mm_dd)
            return True
        except Exception:
            pass
    return False


def _tick_vilnius_checkbox(driver):
    xps = [
        "//label[contains(., 'Vilnius')]",
        "//*[contains(., 'Vilnius') and (self::label or self::span or self::div)]",
    ]
    for xp in xps:
        try:
            el = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, xp)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
            time.sleep(0.3)
            return True
        except Exception:
            pass

    for css in ["input[id*='viln' i]", "input[name*='viln' i]", "input[value*='viln' i]"]:
        try:
            cb = driver.find_element(By.CSS_SELECTOR, css)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cb)
            if not cb.is_selected():
                driver.execute_script("arguments[0].click();", cb)
            return True
        except Exception:
            pass
    return False


def _click_search(driver):
    candidates = [
        (By.XPATH, "//button[contains(., 'Paieška')]"),
        (By.XPATH, "//input[@type='submit' and contains(@value, 'Paieška')]") ,
        (By.CSS_SELECTOR, "button[type='submit']"),
    ]
    for by, sel in candidates:
        try:
            _safe_click(driver, by, sel, timeout=8)
            time.sleep(0.8)
            return True
        except Exception:
            pass
    return False


def _parse_news_rows(driver):
    rows = []
    selectors = ["table tbody tr", "div.table-responsive table tbody tr", ".table tbody tr", "table tr"]
    table_rows = []
    for sel in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            els = [e for e in els if (e.text or "").strip()]
            if len(els) >= 5:
                table_rows = els
                break
        except Exception:
            pass

    if not table_rows:
        return rows

    for tr in table_rows:
        try:
            tds = tr.find_elements(By.CSS_SELECTOR, "td")
            if not tds:
                continue

            link = ""
            title = ""
            try:
                a = tr.find_element(By.CSS_SELECTOR, "a")
                link = a.get_attribute("href") or ""
                title = (a.text or "").strip()
            except Exception:
                title = (tds[0].text if tds else tr.text).strip()

            day_txt = (tds[0].text or "").strip() if len(tds) >= 1 else ""
            time_txt = (tds[1].text or "").strip() if len(tds) >= 2 else ""

            company = ""
            for cand_idx in [3, 4, 5, 2]:
                if len(tds) > cand_idx:
                    company = (tds[cand_idx].text or "").strip()
                    if company:
                        break

            exchange = ""
            lang = ""
            for cand_idx in [6, 7, 5]:
                if len(tds) > cand_idx:
                    maybe = (tds[cand_idx].text or "").strip()
                    if maybe in {"VLN", "RIG", "TAL"}:
                        exchange = maybe
                    if maybe.lower() in {"lt", "lv", "et", "en"}:
                        lang = maybe.lower()

            rows.append({
                "Diena": day_txt,
                "Laikas": time_txt,
                "Nasdaq_antraštė": title,
                "Nasdaq_nuoroda": link,
                "Nasdaq_bendrovė": company,
                "Birža": exchange,
                "Kalba": lang,
            })
        except Exception:
            continue
    return rows


def _nasdaq_dt(row) -> pd.Timestamp:
    d = str(row.get("Diena", "")).strip()
    t = str(row.get("Laikas", "")).strip()
    s = f"{d} {t}".strip()
    return pd.to_datetime(s, errors="coerce")


def scrape_nasdaq_vilnius_news_with_full_text(start_date: date, end_date: date, progress=None) -> pd.DataFrame:
    if progress:
        progress("Nasdaq Baltic: renkami Vilniaus pranešimai ir pilni tekstai...")
    driver = _init_driver()
    try:
        driver.get(NASDAQ_NEWS_URL)
        WebDriverWait(driver, 25).until(lambda d: d.execute_script("return document.readyState") == "complete")
        _try_accept_cookies(driver)

        ok_vln = _tick_vilnius_checkbox(driver)
        if progress and not ok_vln:
            progress("Nasdaq: nepavyko patikimai pažymėti 'Vilnius' varnelės.")

        _set_date_input(driver, "Nuo", start_date.strftime("%Y-%m-%d"))
        _set_date_input(driver, "Iki", end_date.strftime("%Y-%m-%d"))
        _click_search(driver)
        time.sleep(1.2)

        items = _parse_news_rows(driver)
        df_n = pd.DataFrame(items)
        if df_n.empty:
            return pd.DataFrame(columns=[
                "Diena", "Laikas", "Nasdaq_antraštė", "Nasdaq_nuoroda", "Nasdaq_bendrovė", "Birža", "Kalba",
                "Nasdaq_pilna_antraštė", "Nasdaq_pilnas_tekstas", "__dt",
            ])

        df_n["__dt"] = df_n.apply(_nasdaq_dt, axis=1)
        cache = {}
        urls = df_n["Nasdaq_nuoroda"].fillna("").astype(str).unique().tolist()
        urls = [u.strip() for u in urls if u and u.strip().lower().startswith("http")]

        for url in urls:
            try:
                t, c = _open_new_tab_and_get(driver, url, timeout=25)
                cache[url] = (t or "", c or "")
            except Exception:
                cache[url] = ("", "")

        def _get_cached_title(row):
            u = (row.get("Nasdaq_nuoroda") or "").strip()
            t = cache.get(u, ("", ""))[0]
            return t if t else (row.get("Nasdaq_antraštė") or "")

        def _get_cached_text(row):
            u = (row.get("Nasdaq_nuoroda") or "").strip()
            return cache.get(u, ("", ""))[1]

        df_n["Nasdaq_pilna_antraštė"] = df_n.apply(_get_cached_title, axis=1)
        df_n["Nasdaq_pilnas_tekstas"] = df_n.apply(_get_cached_text, axis=1)
        return df_n
    finally:
        driver.quit()


def build_nasdaq_map_for_companies(df_first_north: pd.DataFrame, df_nasdaq_news: pd.DataFrame, max_items=5) -> dict:
    out = {}
    if df_nasdaq_news is None or df_nasdaq_news.empty:
        for b in df_first_north["Bendrovė"].dropna().unique():
            out[b] = ""
        return out

    news = df_nasdaq_news.copy()
    news["__title"] = news["Nasdaq_antraštė"].fillna("").astype(str)
    news["__company"] = news["Nasdaq_bendrovė"].fillna("").astype(str)

    for bendrove in df_first_north["Bendrovė"].dropna().unique():
        hits = []
        for _, r in news.iterrows():
            title = r["__title"]
            comp = r["__company"]
            if atitinka(bendrove, title) or atitinka(bendrove, comp):
                url = (r.get("Nasdaq_nuoroda") or "").strip()
                t = (r.get("Nasdaq_antraštė") or "").strip()
                if not t:
                    continue
                if url:
                    hits.append(f'<a href="{url}" target="_blank">{t}</a>')
                else:
                    hits.append(t)

        seen = set()
        hits_unique = []
        for h in hits:
            key = re.sub(r"\s+", " ", re.sub(r"<.*?>", "", h)).strip().lower()
            if key and key not in seen:
                seen.add(key)
                hits_unique.append(h)
        out[bendrove] = "\n".join(hits_unique[:max_items])
    return out


def _to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0)


def filter_traded_or_has_news(df_fn: pd.DataFrame) -> pd.DataFrame:
    apyv = _to_num(df_fn.get("Apyvarta", pd.Series([0] * len(df_fn))))
    sand = _to_num(df_fn.get("Sand.", pd.Series([0] * len(df_fn))))
    kiek = _to_num(df_fn.get("Kiekis", pd.Series([0] * len(df_fn))))
    traded = (apyv > 0) | (sand > 0) | (kiek > 0)
    news_txt = df_fn.get("Nasdaq pranešimai", pd.Series([""] * len(df_fn))).fillna("").astype(str).str.strip()
    has_news = news_txt != ""
    return df_fn[traded | has_news].copy()


def spalvinti_pokycio_abs_gradienta(col):
    """
    Pok.% stulpelio spalvinimas tik labiausiai išsiskiriančioms reikšmėms.

    Logika:
    - vidurinės reikšmės tarp Q25 ir Q75 neakcentuojamos;
    - didesni teigiami pokyčiai spalvinami švelniu žaliu gradientu;
    - didesni neigiami pokyčiai spalvinami švelniu raudonu gradientu;
    - gradientas skaičiuojamas pagal konkrečioje lentelėje esančias reikšmes.
    """
    s = pd.Series(col).astype(str)
    s = (
        s.str.replace("−", "-", regex=False)   # unicode minus
         .str.replace("–", "-", regex=False)
         .str.replace("%", "", regex=False)
         .str.replace(",", ".", regex=False)
         .str.strip()
    )
    col_num = pd.to_numeric(s, errors="coerce")
    valid = col_num.dropna()

    if valid.empty:
        return [""] * len(col_num)

    q25 = valid.quantile(0.25)
    q75 = valid.quantile(0.75)
    min_val = valid.min()
    max_val = valid.max()

    def _interp_hex(start_hex, end_hex, intensity):
        intensity = max(0, min(float(intensity), 1))
        start_rgb = mcolors.to_rgb(start_hex)
        end_rgb = mcolors.to_rgb(end_hex)
        rgb = tuple(
            start_rgb[i] + (end_rgb[i] - start_rgb[i]) * intensity
            for i in range(3)
        )
        return mcolors.to_hex(rgb)

    styles = []
    eps = 1e-9

    for v in col_num:
        if pd.isna(v):
            styles.append("")
            continue

        # Vidurinės reikšmės neakcentuojamos.
        if q25 < v < q75:
            styles.append("")
            continue

        if v > 0 and max_val > q75:
            raw = (v - q75) / max(max_val - q75, eps)
            intensity = min(max(raw, 0), 1)
            bg = _interp_hex("#f4fbf4", "#9bd29b", intensity)
            color = "#12351d"

        elif v < 0 and min_val < q25:
            raw = (q25 - v) / max(q25 - min_val, eps)
            intensity = min(max(raw, 0), 1)
            bg = _interp_hex("#fbf4f4", "#e09a9a", intensity)
            color = "#4a1111"

        else:
            styles.append("")
            continue

        font_weight = 500 + int(intensity * 250)
        styles.append(
            f"background-color: {bg} !important; "
            f"color: {color} !important; "
            f"font-weight: {font_weight} !important;"
        )

    return styles
def spalvinti_izvalga(val):
    spalva = IZVALGU_SPALVOS.get(val, "")
    return f"background-color: {spalva}; font-weight: bold" if spalva else ""


def render_lentele(df_: pd.DataFrame, caption: str):
    df_ = df_.copy()
    if "Pok.%" in df_.columns:
        df_ = df_.sort_values(by="Pok.%", key=lambda x: pd.to_numeric(x, errors="coerce").abs(), ascending=False)
    cols = [
        "Trumpinys", "Bendrovė", "Atid.", "Paskutinė kaina", "Pok.%", "Apyvarta", "Kiekis",
        "Sand.", "Kategorija", "Naujiena", "Verslo žinios", "Įžvalgos",
    ]
    for c in cols:
        if c not in df_.columns:
            df_[c] = ""
    df_ = df_[cols].copy()
    # Paliekame Pok.% kaip skaitinį stulpelį, kad Styler.apply galėtų patikimai spalvinti.
    # Rodymo formatavimas daromas žemiau per .format({"Pok.%": "{:.2f}"}).
    df_["Pok.%"] = pd.to_numeric(df_["Pok.%"], errors="coerce")

    return (
        df_.style
        .set_caption(caption)
        .set_table_styles([
            {"selector": "caption", "props": [("caption-side", "top"), ("font-weight", "bold"), ("font-size", "14px"), ("color", "#333"), ("padding", "8px")]},
            {"selector": "th", "props": [("background-color", "#f2f2f2"), ("font-weight", "bold"), ("text-align", "center")]},
            {"selector": "td", "props": [("text-align", "left"), ("font-size", "12px")]},
            {"selector": "tbody tr:nth-child(even) td", "props": [("background-color", "#fafafa")]},
            {"selector": "tbody tr:nth-child(odd) td", "props": [("background-color", "#ffffff")]},
        ])
        .format({"Atid.": "{:.2f}", "Paskutinė kaina": "{:.2f}", "Pok.%": "{:.2f}", "Apyvarta": "{:.2f}", "Kiekis": "{:.0f}", "Sand.": "{:.0f}"})
        .apply(spalvinti_pokycio_abs_gradienta, subset=["Pok.%"])
        .map(spalvinti_izvalga, subset=["Įžvalgos"])
        .hide(axis="index")
        .set_properties(subset=["Naujiena", "Verslo žinios", "Įžvalgos"], **{"white-space": "pre-line"})
    )


def render_first_north(df_: pd.DataFrame, caption: str):
    df_ = df_.copy()
    if "Pok.%" in df_.columns:
        df_ = df_.sort_values(by="Pok.%", key=lambda x: pd.to_numeric(x, errors="coerce").abs(), ascending=False)
    cols = [
        "Trumpinys", "Bendrovė", "Atid.", "Paskutinė kaina", "Pok.%", "Apyvarta", "Kiekis",
        "Sand.", "Sąrašas/segmentas", "Naujiena", "Verslo žinios",
    ]
    for c in cols:
        if c not in df_.columns:
            df_[c] = ""
    df_ = df_[cols].copy()
    # Paliekame Pok.% kaip skaitinį stulpelį, kad Styler.apply galėtų patikimai spalvinti.
    # Rodymo formatavimas daromas žemiau per .format({"Pok.%": "{:.2f}"}).
    df_["Pok.%"] = pd.to_numeric(df_["Pok.%"], errors="coerce")

    return (
        df_.style
        .set_caption(caption)
        .set_table_styles([
            {"selector": "caption", "props": [("caption-side", "top"), ("font-weight", "bold"), ("font-size", "14px"), ("color", "#333"), ("padding", "8px")]},
            {"selector": "th", "props": [("background-color", "#f2f2f2"), ("font-weight", "bold"), ("text-align", "center")]},
            {"selector": "td", "props": [("text-align", "left"), ("font-size", "12px")]},
            {"selector": "tbody tr:nth-child(even) td", "props": [("background-color", "#fafafa")]},
            {"selector": "tbody tr:nth-child(odd) td", "props": [("background-color", "#ffffff")]},
        ])
        .format({"Atid.": "{:.2f}", "Paskutinė kaina": "{:.2f}", "Pok.%": "{:.2f}", "Apyvarta": "{:.2f}", "Kiekis": "{:.0f}", "Sand.": "{:.0f}"})
        .apply(spalvinti_pokycio_abs_gradienta, subset=["Pok.%"])
        .hide(axis="index")
        .set_properties(subset=["Naujiena", "Verslo žinios"], **{"white-space": "pre-line"})
    )


def ivertink_izvalga(row):
    if abs(row.get("Pok.%", 0)) > 3 and row.get("Apyvarta", 0) > 100000 and row.get("Kiekis", 0) > 20000:
        return "🔥 Staigus aktyvumas su didele apimtimi"
    elif abs(row.get("Pok.%", 0)) > 3 and row.get("Apyvarta", 0) > 50000 and row.get("Kiekis", 0) > 10000 and row.get("Sand.", 0) > 50:
        return "🚨 Didelis poveikis rinkai"
    elif abs(row.get("Pok.%", 0)) > 2 and row.get("Apyvarta", 0) < 500 and row.get("Kiekis", 0) < 1000 and row.get("Sand.", 0) <= 5:
        return "⚠️ Mažas likvidumas + kainos pokytis"
    elif abs(row.get("Pok.%", 0)) < 1 and row.get("Apyvarta", 0) > 100000 and row.get("Sand.", 0) > 100:
        return "🔍 Didelė apyvarta be kainos pokyčio"
    elif row.get("Sand.", 0) > 150 and abs(row.get("Pok.%", 0)) < 0.5:
        return "🔁 Aktyvumas be kainos pokyčio"
    return ""


def sujungti_naujienas_keli(df_imones: pd.DataFrame, df_news: pd.DataFrame) -> pd.DataFrame:
    df_out = df_imones.copy()
    df_out["Bendrovė_norm"] = df_out["Bendrovė"].astype(str).map(normalize)
    if df_news is None or df_news.empty:
        df_out["Naujiena"] = ""
        df_out["Kategorija"] = ""
        return df_out

    news_keys = df_news["Bendrovė_norm"].dropna().unique().tolist()
    naujienos_list, kategorijos_list = [], []

    for _, row in df_out.iterrows():
        key = row["Bendrovė_norm"]
        candidates = []
        for nk in news_keys:
            if not nk:
                continue
            if key and (key in nk or nk in key or SequenceMatcher(None, key, nk).ratio() >= 0.80):
                candidates.append(nk)

        if candidates:
            subset = df_news[df_news["Bendrovė_norm"].isin(candidates)].copy()
            joined_news = "\n".join(subset["Naujiena"].tolist())
            seen = set()
            ordered_cats = [c for c in subset["Kategorija"].tolist() if not (c in seen or seen.add(c))]
            joined_cats = ", ".join([c for c in ordered_cats if c])
        else:
            joined_news = ""
            joined_cats = ""

        naujienos_list.append(joined_news)
        kategorijos_list.append(joined_cats)

    df_out["Naujiena"] = naujienos_list
    df_out["Kategorija"] = kategorijos_list
    return df_out


def _news_dedup_key(url: str, title: str, text: str) -> str:
    url = (url or "").strip().lower()
    if url:
        return url
    base = (normalize(title or "") + "||" + normalize((text or "")[:200]))
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def sudeti_visas_naujienas_distinct(df_imones: pd.DataFrame, df_crib: pd.DataFrame, df_vz_raw: pd.DataFrame, df_nasdaq_raw: pd.DataFrame) -> pd.DataFrame:
    imones_df = df_imones[["Bendrovė"]].dropna().drop_duplicates().copy()
    imones_df["Bendrovė_norm"] = imones_df["Bendrovė"].astype(str).map(normalize)

    crib = df_crib.copy() if df_crib is not None and not df_crib.empty else pd.DataFrame()
    if not crib.empty and "Bendrovė_norm" not in crib.columns:
        crib["Bendrovė_norm"] = crib["Bendrovė"].astype(str).map(normalize)

    vz_raw = df_vz_raw.copy() if df_vz_raw is not None and not df_vz_raw.empty else pd.DataFrame()
    nasdaq_raw = df_nasdaq_raw.copy() if df_nasdaq_raw is not None and not df_nasdaq_raw.empty else pd.DataFrame()

    out_rows = []
    for _, im in imones_df.iterrows():
        bend = im["Bendrovė"]
        key = im["Bendrovė_norm"]
        seen_keys = set()

        if not crib.empty and key:
            nk_list = [nk for nk in crib["Bendrovė_norm"].dropna().unique()
                       if (key in nk or nk in key or SequenceMatcher(None, key, nk).ratio() >= 0.80)]
            if nk_list:
                sub = crib[crib["Bendrovė_norm"].isin(nk_list)].copy().sort_values("Published_dt", ascending=False)
                for _, r in sub.iterrows():
                    t = str(r.get("Pilna_antraštė", "")).strip()
                    txt = str(r.get("Pilnas_tekstas", "")).strip()
                    url = (r.get("Nuoroda", "") or "").strip()
                    k = _news_dedup_key(url, t, txt)
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    label = ""
                    if t:
                        label += f"<strong>{t}</strong>\n"
                    if txt:
                        label += f"{txt}\n"
                    if url:
                        label += f'\nŠaltinis: <a href="{url}" target="_blank">{url}</a>'
                    out_rows.append({"Bendrovė": bend, "Kategorija": r.get("Kategorija", ""), "Data": pd.to_datetime(r.get("Published_dt", None), errors="coerce"), "Turinys": label})

        if not vz_raw.empty:
            matches = vz_raw[vz_raw["Antraštė"].apply(lambda s: atitinka(bend, s))]
            if not matches.empty:
                matches = matches.sort_values("Data", ascending=False)
                for _, r in matches.iterrows():
                    t = str(r.get("Antraštė", "")).strip()
                    txt = str(r.get("Pilnas_tekstas", "")).strip()
                    url = (r.get("Nuoroda", "") or "").strip()
                    k = _news_dedup_key(url, t, txt)
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    label = ""
                    if t:
                        label += f"<strong>{t}</strong>\n"
                    if txt:
                        label += f"{txt}\n"
                    if url:
                        label += f'\nŠaltinis: <a href="{url}" target="_blank">{url}</a>'
                    out_rows.append({"Bendrovė": bend, "Kategorija": "Verslo žinios", "Data": pd.to_datetime(r.get("Data", None), errors="coerce"), "Turinys": label})

        if not nasdaq_raw.empty:
            sub = nasdaq_raw[
                nasdaq_raw["Nasdaq_antraštė"].apply(lambda s: atitinka(bend, str(s))) |
                nasdaq_raw["Nasdaq_bendrovė"].apply(lambda s: atitinka(bend, str(s)))
            ].copy()
            if not sub.empty:
                sub = sub.sort_values("__dt", ascending=False)
                for _, r in sub.iterrows():
                    t = str(r.get("Nasdaq_pilna_antraštė", "") or r.get("Nasdaq_antraštė", "")).strip()
                    txt = str(r.get("Nasdaq_pilnas_tekstas", "")).strip()
                    url = (r.get("Nasdaq_nuoroda", "") or "").strip()
                    k = _news_dedup_key(url, t, txt)
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    label = ""
                    if t:
                        label += f"<strong>{t}</strong>\n"
                    if txt:
                        label += f"{txt}\n"
                    if url:
                        label += f'\nŠaltinis: <a href="{url}" target="_blank">{url}</a>'
                    out_rows.append({"Bendrovė": bend, "Kategorija": "Nasdaq Baltic (Vilnius)", "Data": pd.to_datetime(r.get("__dt", None), errors="coerce"), "Turinys": label})

    df_all = pd.DataFrame(out_rows)
    if not df_all.empty:
        df_all = df_all.sort_values(["Bendrovė", "Data"], ascending=[True, False]).reset_index(drop=True)
        df_all["Data"] = df_all["Data"].apply(lambda x: "" if pd.isna(x) else pd.to_datetime(x).strftime("%Y-%m-%d %H:%M"))
    else:
        df_all = pd.DataFrame(columns=["Bendrovė", "Kategorija", "Data", "Turinys"])
    return df_all


def render_visos_naujienos(df_all: pd.DataFrame, caption="Visos skirtingos naujienos pagal bendrovę"):
    if df_all is None or df_all.empty:
        df_all = pd.DataFrame([{"Bendrovė": "—", "Kategorija": "", "Data": "", "Turinys": "Naujienų nerasta šiame periode."}])
    return (
        df_all.style
        .set_caption(caption)
        .set_table_styles([
            {"selector": "caption", "props": [("caption-side", "top"), ("font-weight", "bold"), ("font-size", "14px"), ("color", "#333"), ("padding", "8px")]},
            {"selector": "th", "props": [("background-color", "#f2f2f2"), ("font-weight", "bold"), ("text-align", "left")]},
            {"selector": "td", "props": [("text-align", "left"), ("font-size", "12px")]},
        ])
        .hide(axis="index")
        .set_properties(subset=["Turinys"], **{"white-space": "pre-line"})
    )


def build_html_report(styled_akcijos, styled_obligacijos, styled_first_north, styled_visos) -> str:
    return f"""
<!DOCTYPE html>
<html lang="lt">
<head>
    <meta charset="UTF-8">
    <title>Rinkos ataskaita</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        table {{ border-collapse: collapse; width: 100%; margin-bottom: 25px; }}
        th, td {{ border: 1px solid #ddd; padding: 6px; vertical-align: top; }}
        a {{ color: #0645ad; }}
    </style>
</head>
<body>
    {styled_akcijos.to_html()}
    <br><hr><br>
    {styled_obligacijos.to_html()}
    <br><hr><br>
    {styled_first_north.to_html()}
    <br><hr><br>
    {styled_visos.to_html()}
</body>
</html>
"""



def _date_from_any(value):
    """Paverčia įvairias datos reikšmes į date arba None."""
    if value is None or pd.isna(value):
        return None
    try:
        return pd.to_datetime(value, errors="coerce").date()
    except Exception:
        return None


def _source_has_successful_scrape_covering(source: str, start_date: date, end_date: date) -> bool:
    """
    Patikrina market_news_scrape_log, ar pasirinktas intervalas jau buvo scrapintas.
    Tai svarbu ir tais atvejais, kai tam laikotarpiui naujienų nebuvo: tada DB įrašų nėra,
    bet logas leidžia nebescrapinti to paties intervalo vėl ir vėl.
    """
    try:
        url = _supabase_rest_url("market_news_scrape_log")
        params = {
            "select": "date_from,date_to,status",
            "source": f"eq.{source}",
            "status": "eq.success",
            "date_from": f"lte.{start_date}",
            "date_to": f"gte.{end_date}",
            "limit": "1",
        }
        with _http_client() as client:
            response = client.get(url, headers=_supabase_headers(), params=params)
            response.raise_for_status()
            data = response.json() or []
        return len(data) > 0
    except Exception:
        # Jei logų patikrinti nepavyko, geriau nesustabdyti ataskaitos ir naudoti atsarginę logiką.
        return False


def _source_has_recent_successful_scrape(source: str, start_date: date, end_date: date, max_age_hours: int = 6) -> bool:
    """
    Patikrina, ar gyvas laikotarpis neseniai jau buvo scrapintas.
    Naudojamas tam, kad spaudžiant tas pačias datas programa neitų scrapinti kiekvieną kartą.

    Reikalinga, kad market_news_scrape_log lentelėje būtų created_at stulpelis
    su Supabase default now(). Jei created_at nėra, funkcija saugiai grąžina False.
    """
    try:
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
        url = _supabase_rest_url("market_news_scrape_log")
        params = {
            "select": "date_from,date_to,status,created_at",
            "source": f"eq.{source}",
            "status": "eq.success",
            "date_from": f"lte.{start_date}",
            "date_to": f"gte.{end_date}",
            "created_at": f"gte.{cutoff}",
            "order": "created_at.desc",
            "limit": "1",
        }
        with _http_client() as client:
            response = client.get(url, headers=_supabase_headers(), params=params)
            response.raise_for_status()
            data = response.json() or []
        return len(data) > 0
    except Exception:
        return False


def _normalize_cached_news_columns(df_cached: pd.DataFrame, source: str) -> pd.DataFrame:
    """Supabase stulpelius paverčia į seną struktūrą, kurios laukia ataskaitos logika."""
    if df_cached is None or df_cached.empty:
        if source == "crib":
            return pd.DataFrame(columns=[
                "Bendrovė", "Bendrovė_norm", "Kategorija", "Naujiena", "Published_dt",
                "Nuoroda", "Pilna_antraštė", "Pilnas_tekstas",
            ])
        if source == "vz":
            return pd.DataFrame(columns=["Antraštė", "Nuoroda", "Data", "Pilnas_tekstas"])
        return pd.DataFrame()

    df = df_cached.copy()

    if source == "crib" and "source" in df.columns:
        df = df.rename(columns={
            "company": "Bendrovė",
            "company_norm": "Bendrovė_norm",
            "category": "Kategorija",
            "title": "Pilna_antraštė",
            "url": "Nuoroda",
            "published_at": "Published_dt",
            "content": "Pilnas_tekstas",
        })
        df["Published_dt"] = pd.to_datetime(df.get("Published_dt"), errors="coerce")
        df["Naujiena"] = df.apply(
            lambda r: (
                f'<a href="{r.get("Nuoroda", "")}" target="_blank">'
                f'{pd.to_datetime(r.get("Published_dt")).strftime("%Y-%m-%d %H:%M")} – {r.get("Pilna_antraštė", "")}</a>'
                if str(r.get("Nuoroda", "")).strip() and pd.notna(r.get("Published_dt"))
                else str(r.get("Pilna_antraštė", ""))
            ),
            axis=1,
        )
        if "Bendrovė_norm" not in df.columns or df["Bendrovė_norm"].fillna("").eq("").all():
            df["Bendrovė_norm"] = df["Bendrovė"].astype(str).map(normalize)
        return df

    if source == "vz" and "source" in df.columns:
        df = df.rename(columns={
            "title": "Antraštė",
            "url": "Nuoroda",
            "published_at": "Data",
            "content": "Pilnas_tekstas",
        })
        df["Data"] = pd.to_datetime(df.get("Data"), errors="coerce").dt.date
        return df

    return df


def _load_cached_news_normalized(source: str, start_date: date, end_date: date) -> pd.DataFrame:
    cached = load_news_df(source, start_date, end_date)
    return _normalize_cached_news_columns(cached, source)


def _merge_and_dedup_news(existing: pd.DataFrame, fresh: pd.DataFrame, source: str) -> pd.DataFrame:
    frames = [x for x in [existing, fresh] if x is not None and not x.empty]
    if not frames:
        return _normalize_cached_news_columns(pd.DataFrame(), source)
    out = pd.concat(frames, ignore_index=True)
    if source == "crib":
        if "Nuoroda" in out.columns:
            out = out.drop_duplicates(subset=["Nuoroda"], keep="last")
        else:
            out = out.drop_duplicates()
        if "Published_dt" in out.columns:
            out["Published_dt"] = pd.to_datetime(out["Published_dt"], errors="coerce")
            out = out.sort_values("Published_dt", ascending=False)
    elif source == "vz":
        if "Nuoroda" in out.columns:
            out = out.drop_duplicates(subset=["Nuoroda"], keep="last")
        else:
            out = out.drop_duplicates()
        if "Data" in out.columns:
            out["Data"] = pd.to_datetime(out["Data"], errors="coerce").dt.date
            out = out.sort_values("Data", ascending=False)
    return out.reset_index(drop=True)


def _scrape_news_source(source: str, scrape_func, start_date: date, end_date: date, df_stat: pd.DataFrame = None, progress=None) -> pd.DataFrame:
    """Paleidzia atitinkamo saltinio scraperi ir grazina jo DataFrame."""
    if source == "vz":
        _, fresh = scrape_func(start_date, end_date, df_stat, progress=progress)
        return fresh
    return scrape_func(start_date, end_date, progress=progress)




def _vz_scrape_cutoff_date() -> date:
    """VŽ naujienas leidžiame scrapinti tik už paskutines 14 dienų."""
    return date.today() - timedelta(days=14)


def _can_scrape_vz_period(period_start: date, period_end: date) -> bool:
    """True, jei bent dalis periodo patenka į paskutines 14 dienų."""
    return period_end >= _vz_scrape_cutoff_date()

def get_news_with_supabase_cache(source: str, start_date: date, end_date: date, scrape_func, df_stat: pd.DataFrame = None, progress=None) -> pd.DataFrame:
    """
    CRIB ir VŽ cache logika:

    - Visada pirmiausia skaitoma Supabase DB.
    - CRIB gali būti scrapinamas istorijai ir gyvoms dienoms pagal cache / log taisykles.
    - VŽ NIEKADA nescrapinama už periodą, kurio pabaiga senesnė nei 14 dienų.
      Tokiu atveju grąžinami tik DB esantys VŽ įrašai.
    - Jei VŽ intervalas mišrus, sena dalis imama tik iš DB, o scrapinti leidžiama tik
      nuo paskutinių 14 dienų ribos.
    - Vakar ir šiandien laikomos "gyvomis" dienomis, bet jos atnaujinamos tik tada,
      jei nėra neseno sėkmingo scrape log'o. Pagal nutylėjimą TTL = 6 valandos.
    - Po galimo scrapinimo galutinis rezultatas grąžinamas iš Supabase DB.

    Nasdaq statistika čia nedalyvauja ir į DB nerašoma.
    """
    if start_date is None or end_date is None:
        return _normalize_cached_news_columns(pd.DataFrame(), source)

    today = date.today()
    live_start = today - timedelta(days=1)
    live_refresh_hours = 6

    cached_raw = load_news_df(source, start_date, end_date)
    cached = _normalize_cached_news_columns(cached_raw, source)

    # VŽ taisyklė: jei visas pasirinktas periodas senesnis nei 14 dienų,
    # jokio VŽ scraping'o nedarome. Naudojame tik tai, kas jau yra DB.
    if source == "vz" and not _can_scrape_vz_period(start_date, end_date):
        return cached

    # Jei VŽ periodas mišrus, senesnė nei 14 d. dalis nebus scrapinama.
    # Gali būti scrapinama tik nuo vz_scrape_start_allowed iki end_date.
    vz_scrape_start_allowed = _vz_scrape_cutoff_date() if source == "vz" else start_date

    # Jei visas periodas istorinis, DB užtenka. Tušti istoriniai periodai
    # nebescrapinami, jeigu scrape log rodo, kad jie jau buvo patikrinti.
    if end_date < live_start:
        if cached_raw is not None and not cached_raw.empty:
            return cached
        if _source_has_successful_scrape_covering(source, start_date, end_date):
            return cached

        scrape_start = max(start_date, vz_scrape_start_allowed)
        scrape_end = end_date
        if scrape_start <= scrape_end:
            fresh = _scrape_news_source(
                source,
                scrape_func,
                scrape_start,
                scrape_end,
                df_stat=df_stat,
                progress=progress,
            )
            save_news_df(fresh, source)
            log_scrape(source, scrape_start, scrape_end, "success", len(fresh) if fresh is not None else 0)
        return _load_cached_news_normalized(source, start_date, end_date)

    # Istorinė dalis iki užvakarykščios dienos.
    historical_start = start_date
    historical_end = min(end_date, live_start - timedelta(days=1))

    if historical_start <= historical_end:
        hist_cached_raw = load_news_df(source, historical_start, historical_end)
        hist_has_rows = hist_cached_raw is not None and not hist_cached_raw.empty
        hist_has_log = _source_has_successful_scrape_covering(source, historical_start, historical_end)

        if not hist_has_rows and not hist_has_log:
            scrape_start = max(historical_start, vz_scrape_start_allowed)
            scrape_end = historical_end
            if scrape_start <= scrape_end:
                fresh_hist = _scrape_news_source(
                    source,
                    scrape_func,
                    scrape_start,
                    scrape_end,
                    df_stat=df_stat,
                    progress=progress,
                )
                save_news_df(fresh_hist, source)
                log_scrape(source, scrape_start, scrape_end, "success", len(fresh_hist) if fresh_hist is not None else 0)

    # Gyva dalis: vakar + šiandien. Ji atnaujinama tik kas live_refresh_hours val.
    live_range_start = max(start_date, live_start, vz_scrape_start_allowed)
    live_range_end = end_date

    if live_range_start <= live_range_end:
        live_cached_raw = load_news_df(source, live_range_start, live_range_end)
        live_has_recent_scrape = _source_has_recent_successful_scrape(
            source,
            live_range_start,
            live_range_end,
            max_age_hours=live_refresh_hours,
        )

        # Jei gyvas periodas jau neseniai tikrintas, naudojame DB. Tai reiškia,
        # kad spaudžiant tas pačias datas pakartotinai nebus einama į scraperį.
        if not live_has_recent_scrape:
            fresh_live = _scrape_news_source(
                source,
                scrape_func,
                live_range_start,
                live_range_end,
                df_stat=df_stat,
                progress=progress,
            )
            save_news_df(fresh_live, source)
            log_scrape(source, live_range_start, live_range_end, "success", len(fresh_live) if fresh_live is not None else 0)
        elif live_cached_raw is not None and not live_cached_raw.empty:
            pass

    return _load_cached_news_normalized(source, start_date, end_date)


def build_vz_map_from_df(vz_df: pd.DataFrame, df_stat: pd.DataFrame) -> dict:
    """Sukuria bendrovė -> VŽ nuoroda žemėlapį tiek iš šviežio scrape, tiek iš Supabase struktūros."""
    vz_map = {}
    if vz_df is None or vz_df.empty:
        for imone in df_stat["Bendrovė"].dropna().unique():
            vz_map[imone] = ""
        return vz_map

    title_col = "Antraštė" if "Antraštė" in vz_df.columns else "title"
    url_col = "Nuoroda" if "Nuoroda" in vz_df.columns else "url"

    for imone in df_stat["Bendrovė"].dropna().unique():
        found = ""
        for _, row in vz_df.iterrows():
            title = row.get(title_col, "")
            url = row.get(url_col, "")
            if atitinka(imone, title):
                found = f'<a href="{url}" target="_blank">{title}</a>' if str(url).strip() else str(title)
                break
        vz_map[imone] = found
    return vz_map

def generate_report(excel_file, filename: str, start_date: date = None, end_date: date = None, progress=None) -> dict:
    if progress:
        progress("Statistics: nuskaitomas Excel failas...")

    df = pd.read_excel(excel_file, sheet_name="VLN")

    file_start, file_end = extract_dates_from_filename(filename)
    start_date = start_date or file_start
    end_date = end_date or file_end

    if start_date is None or end_date is None:
        raise ValueError(
            "Nepavyko nustatyti laikotarpio. Pasirinkite datas rankiniu būdu "
            "arba naudokite failo pavadinimą su YYYYMMDD_YYYYMMDD."
        )

    # Antraštė visada pagal realiai pasirinktą / perduotą laikotarpį.
    caption = f"Rinkos apžvalga ({start_date} – {end_date})"

    if progress:
        progress("Tikrinama Supabase naujienų bazė...")

    # CRIB = Nasdaq / emitentų pranešimai iš CRIB. Naudojame DB cache ir scrapiname tik trūkstamą dalį.
    df_news = get_news_with_supabase_cache(
        "crib",
        start_date,
        end_date,
        scrape_crib_dom_lt,
        progress=progress,
    )

    # VŽ straipsniai. Taip pat naudojame DB cache ir scrapiname tik trūkstamą dalį.
    vz_df = get_news_with_supabase_cache(
        "vz",
        start_date,
        end_date,
        vz_scrape_full,
        df_stat=df,
        progress=progress,
    )
    vz_map = build_vz_map_from_df(vz_df, df)

    # Atskiro Nasdaq naujienų scraperio nebenaudojame, nes CRIB jau yra Nasdaq / emitentų pranešimų šaltinis.
    df_nasdaq = pd.DataFrame(columns=[
        "Diena", "Laikas", "Nasdaq_antraštė", "Nasdaq_nuoroda", "Nasdaq_bendrovė", "Birža", "Kalba",
        "Nasdaq_pilna_antraštė", "Nasdaq_pilnas_tekstas", "__dt",
    ])

    if progress:
        progress("Formuojamos akcijų, obligacijų ir First North lentelės...")

    df_akcijos = df[df["Sąrašas/segmentas"].isin(SEGMENTAI["Akcijos"])].copy()
    df_obligacijos = df[df["Sąrašas/segmentas"].isin(SEGMENTAI["Obligacijos"])].copy()

    # Visose pagrindinėse lentelėse rodome CRIB ir VŽ.
    df_akcijos = sujungti_naujienas_keli(df_akcijos, df_news)
    df_obligacijos = sujungti_naujienas_keli(df_obligacijos, df_news)
    df_obligacijos = df_obligacijos[df_obligacijos["Naujiena"] != ""]

    df_akcijos["Verslo žinios"] = df_akcijos["Bendrovė"].map(vz_map).fillna("")
    df_obligacijos["Verslo žinios"] = df_obligacijos["Bendrovė"].map(vz_map).fillna("")

    df_akcijos["Įžvalgos"] = df_akcijos.apply(ivertink_izvalga, axis=1)
    df_obligacijos["Įžvalgos"] = df_obligacijos.apply(ivertink_izvalga, axis=1)

    df_first_north = df[
        df["Sąrašas/segmentas"].astype(str).str.contains("First North", case=False, na=False)
    ].copy()
    df_first_north = sujungti_naujienas_keli(df_first_north, df_news)
    df_first_north["Verslo žinios"] = df_first_north["Bendrovė"].map(vz_map).fillna("")
    # First North filtravimo funkcija tikrina stulpelį "Nasdaq pranešimai",
    # todėl į jį dedame CRIB / emitentų pranešimus iš DB.
    df_first_north["Nasdaq pranešimai"] = df_first_north["Naujiena"].fillna("")
    df_first_north = filter_traded_or_has_news(df_first_north)

    # Bendra visų naujienų lentelė: CRIB + VŽ. Atskiro Nasdaq naujienų šaltinio nebėra.
    df_visos = sudeti_visas_naujienas_distinct(df, df_news, vz_df, df_nasdaq)

    styled_akcijos = render_lentele(df_akcijos, caption)
    styled_obligacijos = render_lentele(df_obligacijos, "Baltijos skolos VP sąrašas (obligacijos)")
    styled_first_north = render_first_north(
        df_first_north,
        f"First North (prekiauta arba yra CRIB / VŽ naujienų) ({start_date} – {end_date})",
    )
    styled_visos = render_visos_naujienos(
        df_visos,
        "Visos skirtingos naujienos (CRIB + VŽ, pilnas tekstas)",
    )

    html = build_html_report(styled_akcijos, styled_obligacijos, styled_first_north, styled_visos)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "df_raw": df,
        "df_akcijos": df_akcijos,
        "df_obligacijos": df_obligacijos,
        "df_first_north": df_first_north,
        "df_visos": df_visos,
        "df_crib": df_news,
        "df_vz": vz_df,
        "df_nasdaq": df_nasdaq,
        "styled_akcijos": styled_akcijos,
        "styled_obligacijos": styled_obligacijos,
        "styled_first_north": styled_first_north,
        "styled_visos": styled_visos,
        "html": html,
    }


def download_nasdaq_statistics_excel(start_date: date, end_date: date, download_dir="downloads", progress=None):
    """
    Atsisiunčia Nasdaq Baltic statistiką tiesiai iš download endpoint'o, be Selenium.

    SVARBU:
    - Nasdaq statistika nėra saugoma Supabase DB.
    - Kiekvieną kartą ji parsisiunčiama pagal naudotojo pasirinktą start_date / end_date.
    - Grąžinama kaip BytesIO objektas, kad app.py galėtų perduoti į generate_report().
    """
    def log(message):
        if progress:
            progress(message)

    if start_date is None or end_date is None:
        raise ValueError("Reikia nurodyti pradžios ir pabaigos datas.")
    if start_date > end_date:
        raise ValueError("Data 'Nuo' negali būti vėlesnė už datą 'Iki'.")

    start_txt = start_date.strftime("%Y-%m-%d")
    end_txt = end_date.strftime("%Y-%m-%d")

    log(f"Atsisiunčiama Nasdaq Baltic statistika: {start_txt} – {end_txt}...")

    base_url = "https://nasdaqbaltic.com/statistics/lt/statistics/download"
    params = {
        "filter": 1,
        "start": start_txt,
        "end": end_txt,
    }

    try:
        response = requests.get(base_url, params=params, verify=False, timeout=90)
        response.raise_for_status()
    except Exception as e:
        raise RuntimeError(
            "Nepavyko atsisiųsti Nasdaq Baltic statistikos per download endpoint'ą. "
            f"Laikotarpis: {start_txt} – {end_txt}. Klaida: {e}"
        )

    content = response.content or b""
    if len(content) < 1000:
        raise RuntimeError(
            "Nasdaq Baltic grąžino per mažą / tuščią failą. "
            f"Laikotarpis: {start_txt} – {end_txt}. Atsakymo pradžia: {content[:200]!r}"
        )

    excel_file = BytesIO(content)
    filename = f"statistics_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.xlsx"
    excel_file.name = filename
    excel_file.seek(0)

    log(f"Nasdaq statistikos failas atsisiųstas: {filename}")
    return excel_file, filename

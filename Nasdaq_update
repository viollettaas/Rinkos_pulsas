# -*- coding: utf-8 -*-
"""Nasdaq Baltic pranešimų (įskaitant First North) atnaujinimas į market_news."""

import re
import time
from datetime import date, datetime, timedelta

import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from supabase_cache import save_news_df, log_scrape


NASDAQ_NEWS_URL = "https://nasdaqbaltic.com/statistics/lt/news"
MONTHS_LT = {
    "sau": 1, "vas": 2, "kov": 3, "bal": 4, "geg": 5, "bir": 6,
    "lie": 7, "rgp": 8, "rugp": 8, "rgs": 9, "spa": 10, "lap": 11, "gru": 12,
}


def _notify(progress, message):
    if progress:
        progress(message)


def _driver(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1600,1200")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=lt")
    options.add_argument("--ignore-certificate-errors")
    options.binary_location = "/usr/bin/chromium"
    return webdriver.Chrome(options=options)


def _text(driver, element):
    try:
        return (driver.execute_script("return arguments[0].innerText || arguments[0].textContent;", element) or "").strip()
    except Exception:
        return (element.text or "").strip()


def _accept_cookies(driver):
    for by, selector in [
        (By.ID, "CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"),
        (By.CSS_SELECTOR, "button#onetrust-accept-btn-handler"),
        (By.XPATH, "//button[contains(., 'Sutinku')]"),
    ]:
        try:
            WebDriverWait(driver, 3).until(EC.element_to_be_clickable((by, selector))).click()
            return
        except Exception:
            pass


def _set_date(driver, label, value):
    """Pildo ir paprastus, ir komponentų viduje esančius datos laukus."""
    xpaths = [
        f"//label[contains(normalize-space(.), '{label}')]/following::input[1]",
        f"//*[contains(normalize-space(.), '{label}')]/following::input[1]",
    ]
    for xpath in xpaths:
        try:
            input_el = WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.XPATH, xpath)))
            driver.execute_script("""
                const el = arguments[0], value = arguments[1];
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            """, input_el, value.isoformat())
            return True
        except Exception:
            pass
    return False


def _click_search(driver):
    for by, selector in [
        (By.XPATH, "//button[contains(normalize-space(.), 'Paieška')]"),
        (By.CSS_SELECTOR, "button[type='submit']"),
    ]:
        try:
            button = WebDriverWait(driver, 8).until(EC.element_to_be_clickable((by, selector)))
            driver.execute_script("arguments[0].click();", button)
            return True
        except Exception:
            pass
    return False


def _parse_datetime(day_value, time_value):
    day_value = re.sub(r"\s+", " ", str(day_value or "")).strip()
    time_value = re.search(r"\b\d{1,2}:\d{2}\b", str(time_value or ""))
    time_value = time_value.group(0) if time_value else "00:00"
    parsed = pd.to_datetime(f"{day_value} {time_value}", errors="coerce", dayfirst=True)
    if pd.notna(parsed):
        return parsed.to_pydatetime()

    match = re.search(r"(\d{1,2})\s+([A-Za-zĄČĘĖĮŠŲŪŽąčęėįšųūž]+)", day_value)
    if not match:
        return None
    month = MONTHS_LT.get(match.group(2).lower()[:4]) or MONTHS_LT.get(match.group(2).lower()[:3])
    if not month:
        return None
    today = date.today()
    candidate = datetime(today.year, month, int(match.group(1)), *map(int, time_value.split(":")))
    # Gruodžio pranešimai sausį dar gali būti rodomi be metų.
    if candidate.date() > today + timedelta(days=7):
        candidate = candidate.replace(year=today.year - 1)
    return candidate


def _row_value(driver, row, selectors, fallback_index=None):
    for selector in selectors:
        try:
            value = _text(driver, row.find_element(By.CSS_SELECTOR, selector))
            if value:
                return value
        except Exception:
            pass
    if fallback_index is not None:
        try:
            cells = row.find_elements(By.CSS_SELECTOR, "nef-table-cell, td")
            return _text(driver, cells[fallback_index]) if len(cells) > fallback_index else ""
        except Exception:
            pass
    return ""


def _parse_rows(driver):
    records = []
    rows = driver.find_elements(By.CSS_SELECTOR, "nef-table-row.message-row")
    if not rows:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

    for row in rows:
        try:
            day = _row_value(driver, row, [".table-date", ".message-date", "nef-table-cell.table-date"], 0)
            time_value = _row_value(driver, row, [".table-time", ".message-time", "nef-table-cell.table-time"], 1)
            published_at = _parse_datetime(day, time_value)
            if published_at is None:
                continue

            link, title = "", ""
            for selector in ["nef-link[href]", ".table-title a[href]", "a[href]"]:
                try:
                    anchor = row.find_element(By.CSS_SELECTOR, selector)
                    link = anchor.get_attribute("href") or ""
                    title = _text(driver, anchor)
                    if link or title:
                        break
                except Exception:
                    pass
            title = title or _row_value(driver, row, [".table-title", ".message-title"], 2)
            company = _row_value(driver, row, [".table-company", ".table-issuer", ".message-company"], 4)
            if not company:
                # Nasdaq puslapio stulpeliai: diena, laikas, naujiena, dokumentai, įmonė, kalba.
                company = _row_value(driver, row, [], 4)

            if not title or not company:
                continue
            records.append({
                "Nasdaq_bendrovė": company,
                "Nasdaq_antraštė": title,
                "Nasdaq_pilna_antraštė": title,
                "Nasdaq_nuoroda": link,
                "Nasdaq_pilnas_tekstas": "",
                "__dt": published_at,
            })
        except Exception:
            continue
    return records


def update_nasdaq_news(days_back=370, headless=True, progress=None):
    """Suranda Nasdaq Baltic pranešimus, tarp jų First North, ir įrašo tik dublikatais nesančius įrašus."""
    end_date = date.today()
    start_date = end_date - timedelta(days=max(int(days_back), 1))
    driver = _driver(headless=headless)
    try:
        _notify(progress, "Tikrinami Nasdaq Baltic pranešimai, įskaitant First North...")
        driver.get(NASDAQ_NEWS_URL)
        WebDriverWait(driver, 25).until(lambda d: d.execute_script("return document.readyState") == "complete")
        _accept_cookies(driver)
        _set_date(driver, "Nuo", start_date)
        _set_date(driver, "Iki", end_date)
        _click_search(driver)
        time.sleep(2)

        records = _parse_rows(driver)
        records = [r for r in records if start_date <= r["__dt"].date() <= end_date]
        df = pd.DataFrame(records).drop_duplicates(subset=["Nasdaq_nuoroda", "Nasdaq_antraštė", "__dt"])
        inserted = save_news_df(df, "nasdaq") if not df.empty else 0
        try:
            log_scrape("nasdaq", start_date, end_date, "success", len(df))
        except Exception:
            pass
        return {"records_found": len(df), "records_inserted": int(inserted or 0)}
    except Exception as exc:
        try:
            log_scrape("nasdaq", start_date, end_date, "error", 0, str(exc))
        except Exception:
            pass
        raise
    finally:
        driver.quit()

# -*- coding: utf-8 -*-
"""
manager_transactions_update.py

Papildomas vadovu sandoriu atnaujinimas is jau Supabase issaugotu CRIB pranesimu.

Logika:
- paima naujausius market_news irasus su source='crib';
- atsirenka kategorija 'Pranesimai apie vadovu sandorius';
- patikrina, ar manager_transactions jau turi irasu pagal crib_url;
- jei neturi, atidaro CRIB pranesima Selenium ir nuskaito PDF per jau esama
  save_manager_transactions_from_crib_selenium() funkcija;
- funkcija viduje papildomai praleidzia jau issaugotus PDF.
"""

from __future__ import annotations

import os
os.environ["WDM_SSL_VERIFY"] = "0"

from datetime import date, timedelta
import re
from typing import Any

import pandas as pd

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from supabase_cache import _supabase_headers, _supabase_rest_url, _http_client

try:
    from backfill_manager_transactions_from_crib import save_manager_transactions_from_crib_selenium
except Exception:
    save_manager_transactions_from_crib_selenium = None


MANAGER_CATEGORY_PATTERNS = (
    "pranešimai apie vadovų sandorius",
    "pranesimai apie vadovu sandorius",
    "notifications on transactions concluded by managers",
    "managers' transactions",
)


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _is_manager_category(value: Any) -> bool:
    text = _norm_text(value)
    return any(p in text for p in MANAGER_CATEGORY_PATTERNS)


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


def load_recent_crib_manager_news(days_back: int = 120, limit: int = 500) -> pd.DataFrame:
    """Paima naujausius CRIB pranesimus ir atsirenka vadovu sandoriu kategorija."""
    start_iso = f"{date.today() - timedelta(days=days_back)}T00:00:00"

    url = _supabase_rest_url("market_news")
    params = {
        "select": "id,source,company,category,title,url,published_at,content",
        "source": "eq.crib",
        "published_at": f"gte.{start_iso}",
        "order": "published_at.desc",
        "limit": str(limit),
    }

    with _http_client() as client:
        response = client.get(url, headers=_supabase_headers(), params=params)
        response.raise_for_status()
        data = response.json() or []

    df = pd.DataFrame(data)
    if df.empty:
        return pd.DataFrame(columns=["company", "category", "title", "url", "published_at"])

    for col in ["category", "title", "url", "published_at", "company"]:
        if col not in df.columns:
            df[col] = ""

    mask = df["category"].apply(_is_manager_category)
    df = df[mask].copy()
    df["url"] = df["url"].fillna("").astype(str).str.strip()
    df = df[df["url"] != ""].drop_duplicates(subset=["url"]).reset_index(drop=True)
    return df


def load_existing_manager_crib_urls(limit: int = 10000) -> set[str]:
    """Paima CRIB URL, kuriems manager_transactions lenteleje jau yra PDF/sandoriu irasu."""
    url = _supabase_rest_url("manager_transactions")
    params = {
        "select": "crib_url",
        "crib_url": "not.is.null",
        "limit": str(limit),
    }

    try:
        with _http_client() as client:
            response = client.get(url, headers=_supabase_headers(), params=params)
            response.raise_for_status()
            data = response.json() or []
    except Exception:
        return set()

    return {
        str(row.get("crib_url") or "").strip().lower()
        for row in data
        if str(row.get("crib_url") or "").strip()
    }


def update_manager_transactions_from_market_news(
    days_back: int = 120,
    news_limit: int = 500,
    max_messages: int = 30,
    headless: bool = True,
    progress=None,
) -> dict:
    """
    Papildo manager_transactions lentele pagal market_news jau esancius CRIB vadovu sandoriu pranesimus.

    Returns dict:
        manager_messages_found, manager_messages_skipped_existing,
        manager_messages_processed, manager_transactions_saved, errors
    """
    stats = {
        "manager_messages_found": 0,
        "manager_messages_skipped_existing": 0,
        "manager_messages_processed": 0,
        "manager_transactions_saved": 0,
        "errors": 0,
    }

    if save_manager_transactions_from_crib_selenium is None:
        stats["errors"] = 1
        stats["error_message"] = "Nerastas backfill_manager_transactions_from_crib.py modulis arba save_manager_transactions_from_crib_selenium funkcija."
        return stats

    news_df = load_recent_crib_manager_news(days_back=days_back, limit=news_limit)
    stats["manager_messages_found"] = len(news_df)

    if news_df.empty:
        return stats

    existing_crib_urls = load_existing_manager_crib_urls()
    candidates = []

    for _, row in news_df.iterrows():
        crib_url = str(row.get("url") or "").strip()
        if not crib_url:
            continue
        if crib_url.lower() in existing_crib_urls:
            stats["manager_messages_skipped_existing"] += 1
            continue
        candidates.append(row)
        if len(candidates) >= max_messages:
            break

    if not candidates:
        return stats

    driver = _init_driver(headless=headless)
    try:
        for row in candidates:
            crib_url = str(row.get("url") or "").strip()
            published_at = row.get("published_at")
            try:
                if progress:
                    progress(f"Nuskaitomi vadovų sandorių PDF: {crib_url}")

                saved = save_manager_transactions_from_crib_selenium(
                    driver=driver,
                    crib_url=crib_url,
                    published_at=published_at,
                )
                stats["manager_messages_processed"] += 1
                stats["manager_transactions_saved"] += int(saved or 0)
            except Exception:
                stats["errors"] += 1
                continue
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return stats

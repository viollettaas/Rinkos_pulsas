# -*- coding: utf-8 -*-
"""
vz_update.py

Greitas VŽ naujienų atnaujinimas į Supabase `market_news` lentelę.

Logika:
- pirmą VŽ puslapį paima per requests + BeautifulSoup, be Selenium;
- ištraukia straipsnių antraštes ir URL;
- praleidžia straipsnius, kurių URL jau yra DB;
- praleidžia straipsnius, kurių antraštės neatitinka emitentų sąrašo;
- tik naujiems ir aktualiems straipsniams parsisiunčia pilną tekstą per requests;
- įrašo į Supabase per save_news_df(..., "vz").
"""

import re
from datetime import date, datetime
from typing import Iterable, Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

from rinkos_logika import atitinka, extract_date_from_url
from supabase_cache import save_news_df, _supabase_headers, _supabase_rest_url, _http_client


VZ_HOME_URL = "https://www.vz.lt/"


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.vz.lt/",
}


def _norm_text(value) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _abs_url(url: str) -> str:
    url = _norm_text(url)
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.vz.lt" + url
    return url


def _clean_vz_url(url: str) -> str:
    url = _abs_url(url)
    if not url:
        return ""
    url = url.split("#", 1)[0]
    # Paliekame query tik jei jo tikrai reikia. VŽ nuorodoms dažniausiai nereikia.
    url = url.split("?", 1)[0]
    return url.strip()


def _request_html(url: str, timeout: int = 25) -> str:
    response = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
    response.raise_for_status()
    return response.text or ""


def _extract_vz_items_from_home(html: str, max_articles: int = 80) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()

    # Pirmas bandymas pagal dabartinę VŽ struktūrą.
    for article in soup.select("article.vz-article"):
        a = None
        for sel in [
            "div.vz-article__summary--description a[href]",
            "a[href]",
        ]:
            a = article.select_one(sel)
            if a:
                break
        if not a:
            continue

        url = _clean_vz_url(a.get("href", ""))
        title = _norm_text(a.get_text(" ", strip=True))
        if not title:
            # Kartais antraštė būna aukštesniame konteineryje.
            title = _norm_text(article.get_text(" ", strip=True))
        if not url or not title:
            continue
        if "vz.lt" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        items.append({"Antraštė": title, "Nuoroda": url, "Data": extract_date_from_url(url)})

    # Atsarginis variantas: visi pagrindinio puslapio straipsnių linkai su data URL'e.
    if len(items) < 5:
        for a in soup.select("a[href]"):
            url = _clean_vz_url(a.get("href", ""))
            title = _norm_text(a.get_text(" ", strip=True))
            if not url or not title or "vz.lt" not in url:
                continue
            if not re.search(r"/20\d{2}/\d{2}/\d{2}/", url):
                continue
            if len(title) < 15:
                continue
            if url in seen:
                continue
            seen.add(url)
            items.append({"Antraštė": title, "Nuoroda": url, "Data": extract_date_from_url(url)})
            if len(items) >= max_articles:
                break

    return items[:max_articles]


def _extract_title_and_text_from_article_html(html: str, fallback_title: str = "") -> tuple[str, str]:
    soup = BeautifulSoup(html or "", "html.parser")

    for bad in soup.select("script, style, noscript, iframe, svg, form"):
        try:
            bad.decompose()
        except Exception:
            pass

    title = ""
    for sel in ["h1", "article h1", ".article-title", ".vz-article__title", "title"]:
        el = soup.select_one(sel)
        if el:
            title = _norm_text(el.get_text(" ", strip=True))
            if title:
                break
    if not title:
        title = fallback_title or ""

    candidates = [
        "article",
        "main",
        ".vz-article__content",
        ".vz-article__body",
        ".article-content",
        ".article-body",
        ".content",
    ]

    best = ""
    for sel in candidates:
        for el in soup.select(sel):
            txt = el.get_text("\n", strip=True)
            txt = re.sub(r"\r", "", txt or "")
            txt = re.sub(r"[ \t]+", " ", txt)
            txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
            if len(txt) > len(best):
                best = txt

    if not best:
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        paras = [_norm_text(p) for p in paras if _norm_text(p)]
        best = "\n\n".join(paras[:20])

    return title.strip(), best.strip()


def load_existing_vz_urls(limit: int = 800) -> set[str]:
    """Užkrauna naujausius VŽ URL iš Supabase, kad nereikėtų atidarinėti jau turimų straipsnių."""
    url = _supabase_rest_url("market_news")
    params = {
        "select": "url",
        "source": "eq.vz",
        "order": "published_at.desc",
        "limit": str(limit),
    }

    try:
        with _http_client() as client:
            response = client.get(url, headers=_supabase_headers(), params=params)
            response.raise_for_status()
            data = response.json() or []
    except Exception:
        return set()

    urls = set()
    for row in data:
        u = _clean_vz_url(row.get("url", ""))
        if u:
            urls.add(u)
    return urls


def _issuer_list_from_df(df_issuers: pd.DataFrame) -> list[str]:
    if df_issuers is None or df_issuers.empty or "Bendrovė" not in df_issuers.columns:
        return []
    issuers = (
        df_issuers["Bendrovė"]
        .dropna()
        .astype(str)
        .map(str.strip)
    )
    return [x for x in issuers.unique().tolist() if x]


def _matches_any_issuer(title: str, issuers: Iterable[str]) -> bool:
    title = _norm_text(title)
    if not title:
        return False
    for issuer in issuers:
        try:
            if atitinka(issuer, title):
                return True
        except Exception:
            continue
    return False


def update_vz_news_fast(
    df_issuers: pd.DataFrame,
    existing_url_limit: int = 800,
    max_articles: int = 80,
    progress=None,
) -> dict:
    """
    Greitai atnaujina VŽ naujienas pagal emitentų sąrašą.

    Returns:
        dict: checked, matched, skipped_existing, fetched_details, found, inserted
    """
    def notify(msg: str):
        if progress:
            progress(msg)

    issuers = _issuer_list_from_df(df_issuers)
    if not issuers:
        return {
            "checked": 0,
            "matched": 0,
            "skipped_existing": 0,
            "fetched_details": 0,
            "found": 0,
            "inserted": 0,
            "note": "Emitentų sąrašas tuščias.",
        }

    notify("VŽ: tikrinamas pirmas puslapis per requests...")
    existing_urls = load_existing_vz_urls(limit=existing_url_limit)
    html = _request_html(VZ_HOME_URL, timeout=25)
    items = _extract_vz_items_from_home(html, max_articles=max_articles)

    records = []
    checked = 0
    matched = 0
    skipped_existing = 0
    fetched_details = 0

    for item in items:
        checked += 1
        title = _norm_text(item.get("Antraštė", ""))
        url = _clean_vz_url(item.get("Nuoroda", ""))
        if not title or not url:
            continue

        if url in existing_urls:
            skipped_existing += 1
            continue

        if not _matches_any_issuer(title, issuers):
            continue

        matched += 1
        full_title = title
        full_text = ""
        try:
            article_html = _request_html(url, timeout=25)
            full_title, full_text = _extract_title_and_text_from_article_html(article_html, fallback_title=title)
            fetched_details += 1
        except Exception:
            # Jei pilno teksto nepavyksta gauti, vis tiek išsaugome antraštę ir URL.
            full_title = title
            full_text = ""

        data_url = item.get("Data")
        if data_url is None:
            data_url = date.today()

        records.append({
            "Antraštė": full_title or title,
            "Nuoroda": url,
            "Data": data_url,
            "Pilnas_tekstas": full_text,
        })
        existing_urls.add(url)

    vz_df = pd.DataFrame(records, columns=["Antraštė", "Nuoroda", "Data", "Pilnas_tekstas"])
    inserted = save_news_df(vz_df, "vz") if not vz_df.empty else 0

    return {
        "checked": checked,
        "matched": matched,
        "skipped_existing": skipped_existing,
        "fetched_details": fetched_details,
        "found": len(vz_df),
        "inserted": int(inserted or 0),
    }

# -*- coding: utf-8 -*-
"""Traders.lt Baltijos akcijų rinkos forumo įrašų kaupimas."""
import re
from datetime import date, timedelta
from urllib.parse import parse_qs, urljoin, urlparse
import pandas as pd
import requests
from bs4 import BeautifulSoup
from supabase_cache import save_news_df, log_scrape

BASE = "https://www.traders.lt/"
TOPICS = BASE + "forums.php?m=topics&s=8&o=updated&w=desc&d={}"
DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2}")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RinkosPulsas/1.0)"}

def _get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def _topics(start):
    out = []
    for offset in range(0, 400, 20):
        soup = _get(TOPICS.format(offset))
        page = []
        for a in soup.select('a[href*="forums.php?m=posts"]'):
            title = a.get_text(" ", strip=True)
            ticker = re.match(r"\s*([A-Z0-9]{3,8})\s*-", title)
            if not ticker:
                continue
            row = a.find_parent("tr") or a.parent
            text = row.get_text(" ", strip=True) if row else ""
            dates = re.findall(r"20\d{2}-\d{2}-\d{2}", text)
            if not dates:
                continue
            last = pd.to_datetime(dates[-1], errors="coerce")
            if pd.isna(last):
                continue
            page.append((ticker.group(1), title, urljoin(BASE, a.get("href", "")), last.date()))
        if not page:
            break
        out.extend(x for x in page if x[3] >= start)
        if max(x[3] for x in page) < start:
            break
    return list({(x[0], x[2]): x for x in out}.values())


def _page_number(url):
    """Grąžina temos puslapio numerį; ``n=last`` reiškia nežinomą paskutinį."""
    value = parse_qs(urlparse(url).query).get("n", [""])[0]
    return int(value) if str(value).isdigit() else None


def _topic_page_urls(last_page_url, max_pages=12):
    """Surenka paskutinį ir ankstesnius temos puslapius.

    Traders.lt temos nuoroda įprastai veda į ``n=last``. Pirmajame puslapyje
    iš navigacijos pasiimame realų paskutinio puslapio numerį, tada keliaujame
    atgal. Baigiame anksti, kai įrašai jau senesni už pasirinktą laikotarpį.
    """
    first = _get(last_page_url)
    numbered = [_page_number(urljoin(BASE, a.get("href", "")))
                for a in first.select('a[href*="m=posts"][href*="n="]')]
    numbered = [n for n in numbered if n is not None]
    last_number = max(numbered) if numbered else _page_number(last_page_url)
    if last_number is None:
        return [(last_page_url, first)]

    def page_url(number):
        return re.sub(r"([?&]n=)(?:last|\\d+)", rf"\\g<1>{number}", last_page_url)

    urls = [(page_url(last_number), first)]
    for number in range(last_number - 1, max(0, last_number - max_pages), -1):
        urls.append((page_url(number), None))
    return urls


def _rows_from_page(ticker, topic, page_url, soup, start, end):
    """Iš išskleisto puslapio paima laikotarpio komentarus.

    Senesnis forumo HTML neturi patikimo vieno CSS selektoriaus įrašo tekstui,
    todėl naudojama kiekvieno datos ženklo pozicija: tekstas yra iki kito datos
    ženklo. Tai taip pat išlaiko komentarų eiliškumą puslapyje.
    """
    lines = [x.strip() for x in soup.get_text("\n", strip=True).splitlines() if x.strip()]
    dated = [(i, DATE_RE.search(value)) for i, value in enumerate(lines)]
    dated = [(i, match) for i, match in dated if match]
    rows, dates = [], []
    ignored = re.compile(r"^(Senbuvis|Aktyvusis|Naujokas|Narys|Moderatorius|Prisijungė|Žinučių:|Online būsena)", re.I)

    for position, (line_index, match) in enumerate(dated):
        timestamp = pd.to_datetime(match.group(0), errors="coerce")
        if pd.isna(timestamp):
            continue
        dates.append(timestamp.date())
        if not (start <= timestamp.date() <= end):
            continue
        next_index = dated[position + 1][0] if position + 1 < len(dated) else len(lines)
        content_lines = []
        for value in lines[line_index + 1:next_index]:
            if value.startswith(("Puslapiai:", "Forumas", "Pranešimo redagavimas")) or ignored.match(value):
                continue
            if len(value) > 2 and not re.fullmatch(r"[\d() ]+", value):
                content_lines.append(value)
        body = " ".join(content_lines).strip()
        post_match = re.search(r"#(\d+)", lines[line_index])
        if body:
            rows.append({
                "Ticker": ticker,
                "Tema": topic,
                "Nuoroda": page_url + (f"#{post_match.group(1)}" if post_match else ""),
                "Data": timestamp,
                "Tekstas": body[:12000],
            })
    return rows, dates


def _posts(ticker, topic, url, start, end):
    rows = []
    for page_url, already_loaded in _topic_page_urls(url):
        soup = already_loaded or _get(page_url)
        page_rows, page_dates = _rows_from_page(ticker, topic, page_url, soup, start, end)
        rows.extend(page_rows)
        # Kadangi einame nuo naujausio puslapio atgal, tolesni puslapiai jau
        # nebegali turėti reikiamo laikotarpio įrašų.
        if page_dates and min(page_dates) < start:
            break
    return rows

def update_traders_forum(days_back=45):
    end = date.today(); start = end - timedelta(days=days_back)
    records = []
    for ticker, topic, url, _ in _topics(start):
        try:
            records.extend(_posts(ticker, topic, url, start, end))
        except Exception:
            continue
    df = pd.DataFrame(records).drop_duplicates(subset=["Ticker", "Data", "Tekstas"]) if records else pd.DataFrame()
    if not df.empty:
        df = df.sort_values(["Ticker", "Data"])
    inserted = save_news_df(df, "traders") if not df.empty else 0
    try: log_scrape("traders", start, end, "success", len(df))
    except Exception: pass
    return {"found": len(df), "inserted"

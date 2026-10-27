# -*- coding: utf-8 -*-
"""Traders.lt Baltijos akcijų rinkos forumo įrašų kaupimas."""
import re
import time
from datetime import date, timedelta
from urllib.parse import parse_qs, urljoin, urlparse
import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup
from supabase_cache import save_news_df, log_scrape

BASE = "https://www.traders.lt/"
TOPICS = BASE + "forums.php?m=topics&s=8&o=updated&w=desc&d={}"
DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2}")
POST_ID_RE = re.compile(r"#\s*(\d+)")
PROFILE_SEPARATOR = "|||"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RinkosPulsas/1.0)"}
SESSION = requests.Session()

# Kai kuriuose darbiniuose tinkluose HTTPS srautas tikrinamas per vidinį
# sertifikatą, kurio Windows Python sertifikatų saugykla nepažįsta.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def _get(url):
    """Nuskaito puslapį su pakartojimu, nes forumas riboja greitas užklausas."""
    last_error = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, headers={**HEADERS, "Referer": BASE}, timeout=45, verify=False)
            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {response.status_code}")
            response.raise_for_status()
            time.sleep(0.20)
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Nepavyko nuskaityti {url}: {last_error}")

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


def _page_offset(url):
    """Grąžina Traders.lt temos puslapio įrašų poslinkį (``d`` parametrą)."""
    value = parse_qs(urlparse(url).query).get("d", [""])[0]
    return int(value) if str(value).isdigit() else None


def _topic_page_urls(last_page_url, max_pages=12):
    """Surenka paskutinį ir ankstesnius temos puslapius.

    Traders.lt temos nuoroda įprastai veda į pirmą puslapį. Puslapiavimui jis
    naudoja ne puslapio numerį, o ``d`` poslinkį (po 20 įrašų). Iš pirmojo
    puslapio pasiimame didžiausią poslinkį, tada keliaujame
    atgal. Baigiame anksti, kai įrašai jau senesni už pasirinktą laikotarpį.
    """
    first = _get(last_page_url)
    # Temos pavadinimo nuoroda atidaro pirmą puslapį (be ``d=``), todėl iš
    # puslapiavimo nuorodų būtina paimti tikrą paskutinio puslapio URL.
    offsets = []
    for anchor in first.select('a[href*="m=posts"][href*="d="]'):
        candidate = urljoin(BASE, anchor.get("href", ""))
        offset = _page_offset(candidate)
        if offset is not None:
            offsets.append((offset, candidate))

    last_offset = max((offset for offset, _ in offsets), default=_page_offset(last_page_url))
    if last_offset is None:
        return [(last_page_url, first)]

    last_url = next((candidate for offset, candidate in offsets if offset == last_offset), None)

    def page_url(offset):
        if last_url:
            return re.sub(r"([?&]d=)\d+", rf"\g<1>{offset}", last_url)
        # Atsarginis variantas temoms, kurios turi tik vieną puslapį.
        separator = "&" if "?" in last_page_url else "?"
        return last_page_url + f"{separator}d={offset}"

    # ``first`` yra pirmo puslapio HTML, todėl naujausią puslapį keliame iš naujo.
    urls = [(page_url(last_offset), None)]
    first_offset = 0 if max_pages is None else max(0, last_offset - 20 * (max_pages - 1))
    for offset in range(last_offset - 20, first_offset - 1, -20):
        urls.append((page_url(offset), None))
    return urls


def _rows_from_page(ticker, topic, page_url, soup, start, end):
    """Paima atskirus komentarus pagal jų unikalią ``p=`` nuorodą.

    Kiekvieną komentaro ID pakeičiame vidiniu žymekliu ir tuomet skaitome
    puslapį eilės tvarka. Taip komentaro tekstas prasideda iškart po jo ID ir
    baigiasi prieš kito komentaro datos antraštę; citatoje esanti data naujo
    įrašo nesukuria, nes šalia jos nėra ``p=`` žymeklio.
    """
    post_links = soup.select('a[href*="m=posts"][href*="p="]')
    for link in post_links:
        post_id = parse_qs(urlparse(urljoin(BASE, link.get("href", ""))).query).get("p", [""])[0]
        if post_id:
            link.replace_with(f"\n__TRADERS_POST_{post_id}__\n")

    lines = [value.strip() for value in soup.get_text("\n", strip=True).splitlines() if value.strip()]
    markers = []
    for index, value in enumerate(lines):
        match = re.fullmatch(r"__TRADERS_POST_(\d+)__", value)
        if match:
            markers.append((index, match.group(1)))

    headers = []
    for marker_index, post_id in markers:
        # Datos eilutė yra netoli prieš pat komentaro numerį.
        date_index = next(
            (i for i in range(marker_index - 1, max(-1, marker_index - 6), -1) if DATE_RE.search(lines[i])),
            None,
        )
        if date_index is not None:
            headers.append((date_index, marker_index, post_id))

    rows, dates = [], []
    status_re = re.compile(r"^(Senbuvis|Aktyvusis|Naujokas|Narys|Moderatorius)$", re.I)
    messages_re = re.compile(r"^\d[\d ]* žinutės", re.I)

    for position, (date_index, marker_index, post_id) in enumerate(headers):
        timestamp = pd.to_datetime(DATE_RE.search(lines[date_index]).group(0), errors="coerce")
        if pd.isna(timestamp):
            continue
        dates.append(timestamp.date())
        if not (start <= timestamp.date() <= end):
            continue

        previous_marker = headers[position - 1][1] if position else -1
        profile_lines = lines[max(previous_marker + 1, date_index - 12):date_index]
        status_index = next((i for i, value in enumerate(profile_lines) if status_re.match(value)), None)
        messages_index = next((i for i, value in enumerate(profile_lines) if messages_re.match(value)), None)

        author = profile_lines[status_index - 1] if status_index and status_index > 0 else ""
        status = profile_lines[status_index] if status_index is not None else ""
        messages = profile_lines[messages_index] if messages_index is not None else ""
        location = ""
        if messages_index is not None and messages_index > 0:
            candidate = profile_lines[messages_index - 1]
            if candidate not in {"Online būsena", author, status} and not re.fullmatch(r"[\d() ]+", candidate):
                location = candidate

        next_date_index = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        body_lines = []
        for value in lines[marker_index + 1:next_date_index]:
            if value.startswith(("Puslapiai:", "Forumas", "Norėdami rašyti forume", "© traders.lt")):
                break
            if value in {"Redaguoti", "Cituoti", "Pranešti"}:
                continue
            body_lines.append(value)
        body = "\n".join(body_lines).strip()

        if body:
            # Metaduomenys laikomi title lauke, nes market_news lentelėje nėra
            # atskirų autoriaus stulpelių. Atvaizduojant jie išskiriami į eilutę.
            display_topic = PROFILE_SEPARATOR.join([topic, author, status, location, messages])
            rows.append({
                "Ticker": ticker,
                "Tema": display_topic,
                "Nuoroda": f"{BASE}forums.php?m=posts&p={post_id}",
                "Data": timestamp,
                "Tekstas": body[:12000],
            })
    return rows, dates


def _posts(ticker, topic, url, start, end, max_pages=12):
    rows = []
    for page_url, already_loaded in _topic_page_urls(url, max_pages=max_pages):
        soup = already_loaded or _get(page_url)
        page_rows, page_dates = _rows_from_page(ticker, topic, page_url, soup, start, end)
        rows.extend(page_rows)
        # Kadangi einame nuo naujausio puslapio atgal, tolesni puslapiai jau
        # nebegali turėti reikiamo laikotarpio įrašų.
        if page_dates and min(page_dates) < start:
            break
    return rows

def update_traders_forum(days_back=45, start_date=None, end_date=None, max_post_pages=12):
    """Įkelia forumo komentarus į Supabase.

    ``max_post_pages=None`` naudojamas tik vietiniam istorijos įkėlimui: tada
    kiekviena aktuali tema skaitoma atgal iki pasirinktos pradžios datos.
    """
    end = end_date or date.today()
    start = start_date or (end - timedelta(days=days_back))
    if start > end:
        raise ValueError("Pradžios data negali būti vėlesnė už pabaigos datą.")
    records = []
    failed_topics = 0
    errors = []
    for ticker, topic, url, _ in _topics(start):
        try:
            records.extend(_posts(ticker, topic, url, start, end, max_pages=max_post_pages))
        except Exception as exc:
            failed_topics += 1
            if len(errors) < 10:
                errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
            continue
    df = pd.DataFrame(records).drop_duplicates(subset=["Ticker", "Data", "Tekstas"]) if records else pd.DataFrame()
    if not df.empty:
        df = df.sort_values(["Ticker", "Data"])
    inserted = save_news_df(df, "traders") if not df.empty else 0
    try: log_scrape("traders", start, end, "success", len(df))
    except Exception: pass
    return {"found": len(df), "inserted": inserted, "failed_topics": failed_topics, "errors": errors}

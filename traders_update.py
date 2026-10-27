# -*- coding: utf-8 -*-
"""Traders.lt Baltijos akcijų rinkos forumo įrašų kaupimas."""
import re
import time
from datetime import date, timedelta
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
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
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
}
SESSION = requests.Session()
REQUEST_PAUSE_SECONDS = 1.2

# Kai kuriuose darbiniuose tinkluose HTTPS srautas tikrinamas per vidinį
# sertifikatą, kurio Windows Python sertifikatų saugykla nepažįsta.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def _get(url):
    """Nuskaito puslapį su pakartojimu, nes forumas riboja greitas užklausas."""
    global SESSION
    last_error = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, headers={**HEADERS, "Referer": BASE}, timeout=45, verify=False)
            # Traders.lt kartais pateikia tariamą 404 po daug nuoseklių
            # užklausų. Nauja sesija ir ilgesnė pauzė tokiu atveju leidžia
            # pakartoti tą patį puslapį, o ne prarasti visą emitento temą.
            if response.status_code == 404:
                response.close()
                SESSION.close()
                SESSION = requests.Session()
                raise requests.HTTPError(f"HTTP 404 (bandymas {attempt + 1})")
            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {response.status_code}")
            response.raise_for_status()
            time.sleep(REQUEST_PAUSE_SECONDS)
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Nepavyko nuskaityti {url}: {last_error}")


def _canonical_topic_url(url):
    """Sutvarko temos parametrų eilę į Traders.lt naudojamą formą.

    Kai kuriems seniems puslapiams svetainė nestabiliai atsako į ``m, q, d``
    eiliškumą, nors naršyklėje kanoninė nuoroda yra ``d, m, q``.
    """
    parsed = urlparse(urljoin(BASE, url))
    query = parse_qs(parsed.query)
    topic_id = query.get("q", [""])[0]
    if not topic_id:
        return urljoin(BASE, url)
    pairs = []
    # Net pirmasis puslapis turi būti su ``d=0``. Kai kurioms senesnėms
    # temoms URL be šio parametro serveris per requests grąžina 404.
    offset = query.get("d", ["0"])[0] or "0"
    pairs.append(("d", offset))
    pairs.extend((("m", "posts"), ("q", topic_id)))
    return BASE + "forums.php?" + urlencode(pairs)

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
            page.append((ticker.group(1), title, _canonical_topic_url(a.get("href", "")), last.date()))
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
        candidate = _canonical_topic_url(anchor.get("href", ""))
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

    def is_profile_block_start(values, index):
        """Atpažįsta autoriaus kortelę, kuri forumo HTML kartojama po tekstu.

        Kortelė paprastai yra ``vardas, statusas, [vieta], X žinutės``.
        Jos negalima pridėti prie komentaro turinio.
        """
        if index + 2 >= len(values) or not status_re.match(values[index + 1]):
            return False
        return any(messages_re.match(candidate) for candidate in values[index + 2:index + 7])

    def is_forum_footer_start(values, index):
        """Atpažįsta navigaciją po paskutiniu temos komentaru.

        Išskleistame Traders.lt tekste ji prasideda eilutėmis
        ``Pradžia`` ir ``Aktyvios temos``, o po jų eina puslapių numeriai.
        """
        return (
            values[index] == "Pradžia"
            and index + 1 < len(values)
            and values[index + 1] == "Aktyvios temos"
        )

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
        candidate_lines = lines[marker_index + 1:next_date_index]
        for line_index, value in enumerate(candidate_lines):
            if value.startswith(("Puslapiai:", "Forumas", "Norėdami rašyti forume", "© traders.lt")):
                break
            # Po teksto forumas dar kartą išveda autoriaus šoninę kortelę.
            # Ji prasideda autoriaus vardu, o kitoje eilutėje yra statusas.
            if is_profile_block_start(candidate_lines, line_index):
                break
            if is_forum_footer_start(candidate_lines, line_index):
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
        try:
            soup = already_loaded or _get(page_url)
        except RuntimeError as exc:
            # Vienas laikinai nepasiekiamas puslapis neturi panaikinti visų
            # tos bendrovės jau rastų (ar kituose puslapiuose esančių) įrašų.
            # Kitas senesnis puslapis bus bandomas toliau.
            if "404" in str(exc):
                continue
            raise
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

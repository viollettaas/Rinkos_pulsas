"""
Emitentų atranka pagal CRIB naujienas.

Ši versija pritaikyta Streamlit aplikacijai ir generuoja HTML ataskaitą
pagal Jupyter ataskaitos logiką:
- tas pats pranešimas prie to paties emitento rodomas tik vieną kartą;
- pranešimai nebeskaidomi į atskirus kategorijų blokus;
- kiekvieno emitento pranešimai rodomi chronologine tvarka nuo seniausio iki naujausio;
- prie kiekvieno įrašo rodoma, kokios temos ir konkretūs raktažodžiai aptikti;
- raktažodžiai paryškinami tik tame įraše, kuriame jie suveikė;
- tekstai valomi nuo literalų \n, \r, \t ir ilgų eilučių, kad HTML maketas neiširtų;
- duomenys imami iš Supabase market_news per supabase_cache.load_news_df(source='crib').
"""

import html as html_lib
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from supabase_cache import load_news_df

try:
    from supabase_cache import _http_client, _supabase_headers, _supabase_rest_url
except Exception:
    _http_client = None
    _supabase_headers = None
    _supabase_rest_url = None


# ============================================================
# KATEGORIJŲ / RAKTAŽODŽIŲ KONFIGŪRACIJA
# ============================================================

RAW_CATEGORIES = {
    "Teismo_procesai_tyrimai": [
        "teism", "byl", "ieškin", "ieskin", "ginč", "ginc", "atsakov", "ieškov", "ieskov",
        "arbitraž", "arbitraz", "administracin", "civilin", "baudžiam", "baudziam",
        "prokurat", "ikiteismin", "tyrim", "apskund", "sprendim", "nutart",
        "įsiteisėj", "isiteisej", "bausm", "baud", "sankcij",
    ],
    "Verslo_jungimai_isigijimai_pardavimai": [
        "įsigij", "isigij", "įsigy", "isigy", "pardav", "perleid", "susijung",
        "prijung", "atskyr", "reorganiz", "sandor", "kontrol", "paket", "dukterin",
        "turto pardav", "veiklos pardav", "akcijų pirk", "akciju pirk",
        "M&A", "merger", "acquisition", "spin-off", "spinoff",
    ],
    "Restrukturizavimas_nemokumas": [
        "restruktūriz", "restrukturiz", "nemok", "bankrot", "kreditor", "skol",
        "likvid", "mokum", "pertvark", "optimiz", "veiklos nutrauk", "padalinio uždarym",
        "padalinio uzdarym", "refinans", "covenant",
    ],
    "Finansiniai_neigiami_signalai": [
        "nuostol", "gryn", "neigiam", "reikšming", "reiksming", "esmin", "sumažėj",
        "sumazej", "pajam", "pinig", "nuraš", "nuras", "vertės sumaž", "vertes sumaz",
        "finansin sunkum", "likvidum problem", "mokum problem", "profit warning",
        "impairment", "write-off", "write off", "smuk", "nuosmuk",
    ],
    "Apskaitos_politikos_keitimas": [
        "apskait", "politik", "metod", "koreg", "korekc", "klaid", "tais",
        "peržiūr", "perziur", "retrospektyv", "restatement", "perklasifikav",
    ],
    "Vadovybes_valdymo_organu_pokyciai": [
        "atsistatyd", "atleid", "atšauk", "atsauk", "nutrauk", "paskirt", "laikin",
        "valdyb", "taryb", "direktor", "vadov", "stebėtoj", "stebetoj", "CEO", "CFO",
        "generalin", "finansų direktori", "finansu direktori",
    ],
    "Auditoriaus_pasikeitimas": [
        "audit", "auditor", "audito sutart", "audito įmon", "audito imon", "sąlyginė nuomon",
        "salygine nuomon", "neigiama auditoriaus", "paskirt", "nutrauk", "atsisak",
    ],
}

CATEGORY_ORDER = list(RAW_CATEGORIES.keys())


# ============================================================
# PAGALBINĖS FUNKCIJOS
# ============================================================


def norm_text(value) -> str:
    """Sutvarko tekstą, kad HTML neatsirastų literalūs \n / \r / \t ir ilgos tuščios sekos."""
    if value is None:
        return ""

    s = str(value)
    s = s.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def slugify(value) -> str:
    s = norm_text(value).lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\-_\.]", "", s, flags=re.U)
    return s[:120] or "unknown"


def _parse_single_date_safe(value):
    """
    Datos parsavimas lietuviskai aplinkai.

    Svarbu: CRIB / VZ tekstuose datos daznai buna DD/MM/YYYY arba DD.MM.YYYY.
    Pandas pagal nutylejima dviprasme data 10/06/2026 gali suprasti kaip
    spalio 6 d., todel tokias datas visada interpretuojame day-first.
    ISO datos YYYY-MM-DD paliekamos year-first.
    """
    if value is None:
        return pd.NaT

    try:
        if pd.isna(value):
            return pd.NaT
    except Exception:
        pass

    # Jeigu tai jau Timestamp / datetime / date, jo nebeperinterpretuojame kaip teksto.
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.to_datetime(value, errors="coerce")
    if isinstance(value, date):
        return pd.to_datetime(value, errors="coerce")

    s = norm_text(value)
    if not s:
        return pd.NaT

    # Pašaliname laiko zonos trumpinius, pvz. EEST/EET, nes pandas juos kartais
    # interpretuoja nevienodai arba meta warning. Datos reikšmė CRIB lentelėje jau yra vietiniu laiku.
    s = re.sub(r"\s+(EEST|EET|UTC|GMT)\s*$", "", s, flags=re.I).strip()

    # ISO / Supabase formatai: 2026-06-10, 2026-06-10T08:12:00, 2026-06-10 08:12:00+00:00.
    # Čia niekada negalima taikyti dayfirst, nes YYYY-MM-DD turi būti year-first.
    m_iso = re.match(
        r"^(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})"
        r"(?:[T\s]+(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<sec>\d{2}))?)?",
        s,
    )
    if m_iso:
        try:
            return pd.Timestamp(
                year=int(m_iso.group("y")),
                month=int(m_iso.group("m")),
                day=int(m_iso.group("d")),
                hour=int(m_iso.group("h") or 0),
                minute=int(m_iso.group("mi") or 0),
                second=int(m_iso.group("sec") or 0),
            )
        except Exception:
            return pd.NaT

    # Lietuviski / europiniai formatai: 10/06/2026, 10.06.2026, 10-06-2026, su laiku arba be jo.
    m = re.match(
        r"^(?P<d>\d{1,2})[./-](?P<m>\d{1,2})[./-](?P<y>\d{4})"
        r"(?:\s+(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<sec>\d{2}))?)?",
        s,
    )
    if m:
        day = int(m.group("d"))
        month = int(m.group("m"))
        year = int(m.group("y"))
        hour = int(m.group("h") or 0)
        minute = int(m.group("mi") or 0)
        second = int(m.group("sec") or 0)
        try:
            return pd.Timestamp(year=year, month=month, day=day, hour=hour, minute=minute, second=second)
        except Exception:
            return pd.NaT

    # Lietuviski menesiu pavadinimai, jeigu ateityje ateitu tekstas pvz. "10 birzelio 2026".
    months = {
        "sausio": 1, "sausis": 1,
        "vasario": 2, "vasaris": 2,
        "kovo": 3, "kovas": 3,
        "balandzio": 4, "balandžio": 4, "balandis": 4,
        "geguzes": 5, "gegužes": 5, "gegužės": 5, "geguze": 5, "gegužė": 5,
        "birzelio": 6, "birželio": 6, "birzelis": 6, "birželis": 6,
        "liepos": 7, "liepa": 7,
        "rugpjucio": 8, "rugpjūcio": 8, "rugpjūčio": 8, "rugpjutis": 8, "rugpjūtis": 8,
        "rugsejo": 9, "rugsėjo": 9, "rugsejis": 9, "rugsėjis": 9,
        "spalio": 10, "spalis": 10,
        "lapkricio": 11, "lapkričio": 11, "lapkritis": 11,
        "gruodzio": 12, "gruodžio": 12, "gruodis": 12,
    }
    s_low = _remove_lithuanian_accents(s.lower())
    month_pattern = "|".join(sorted(set(_remove_lithuanian_accents(k) for k in months), key=len, reverse=True))
    m = re.search(rf"(?P<d>\d{{1,2}})\s+(?P<mon>{month_pattern})\s+(?P<y>\d{{4}})", s_low)
    if m:
        try:
            mon_key = m.group("mon")
            month = months.get(mon_key) or months.get(mon_key + "io")
            if month:
                return pd.Timestamp(year=int(m.group("y")), month=int(month), day=int(m.group("d")))
        except Exception:
            return pd.NaT

    # Paskutinis bandymas taip pat dayfirst=True. Nenaudojame pandas default month-first.
    return pd.to_datetime(s, dayfirst=True, errors="coerce")


def parse_dates_safe(series: pd.Series) -> pd.Series:
    """Saugus datų parsavimas iš kelių galimų formatų, prioritetas LT DD/MM/YYYY."""
    if series is None:
        return pd.Series(dtype="datetime64[ns]")

    s = pd.Series(series).copy()
    parsed = s.map(_parse_single_date_safe)
    return pd.to_datetime(parsed, errors="coerce")


def _norm_for_dedup(value) -> str:
    s = norm_text(value).lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _make_news_key(row: pd.Series) -> str:
    """Raktas dublikatams: tas pats URL arba ta pati emitento/antraštės/datos kombinacija."""
    issuer = _norm_for_dedup(row.get("issuer", ""))
    url = _norm_for_dedup(row.get("url", ""))
    title = _norm_for_dedup(row.get("title", ""))
    dt = row.get("date_parsed")
    try:
        date_part = pd.to_datetime(dt, errors="coerce").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        date_part = _norm_for_dedup(row.get("date", ""))[:19]

    if url:
        return f"{issuer}|url|{url}"
    return f"{issuer}|title|{title}|{date_part}"


def keyword_to_regex(token: str) -> re.Pattern:
    """
    Sudaro lankstų regex:
    - frazėms leidžia kelis tarpus;
    - vieno žodžio kamienams ieško žodžio pradžios.
    """
    token = norm_text(token).lower()
    if not token:
        return re.compile(r"a^", flags=re.I | re.U)

    token_escaped = re.escape(token)

    if " " in token:
        token_escaped = token_escaped.replace(r"\ ", r"\s+")
        pattern = token_escaped
    else:
        # Leidžiame lietuviškas ir angliškas galūnes po kamieno.
        pattern = rf"\b{token_escaped}\w*"

    return re.compile(pattern, flags=re.I | re.U)


COMPILED = {
    category: [(token, keyword_to_regex(token)) for token in tokens]
    for category, tokens in RAW_CATEGORIES.items()
}


def find_matches(text: str) -> Tuple[List[str], Dict[str, List[str]], List[str]]:
    """
    Grąžina:
    - matched_categories;
    - matched_keywords_map {kategorija: [raktažodžiai]};
    - flat_keywords.
    """
    text = norm_text(text).lower()
    matched_keywords = defaultdict(list)

    for category, patterns in COMPILED.items():
        seen_tokens = set()
        for token, pattern in patterns:
            if pattern.search(text):
                if token not in seen_tokens:
                    matched_keywords[category].append(token)
                    seen_tokens.add(token)

    matched_categories = [cat for cat in CATEGORY_ORDER if cat in matched_keywords]

    flat_keywords = []
    for category in matched_categories:
        flat_keywords.extend(matched_keywords[category])

    return matched_categories, dict(matched_keywords), flat_keywords


def build_highlight_pattern(tokens: Iterable[str]):
    escaped_tokens = []
    for token in sorted(set(tokens or []), key=lambda x: len(str(x)), reverse=True):
        token = norm_text(token).lower()
        if not token:
            continue
        if " " in token:
            token_esc = re.escape(token).replace(r"\ ", r"\s+")
            escaped_tokens.append(token_esc)
        else:
            escaped_tokens.append(rf"\b{re.escape(token)}\w*")

    if not escaped_tokens:
        return None
    return re.compile("|".join(escaped_tokens), flags=re.I | re.U)


def highlight_keywords(text: str, tokens_to_highlight: Optional[Iterable[str]] = None) -> str:
    if not text:
        return ""

    escaped = html_lib.escape(norm_text(text))
    if not tokens_to_highlight:
        return escaped

    pattern = build_highlight_pattern(tokens_to_highlight)
    if pattern is None:
        return escaped

    return pattern.sub(
        lambda m: f"<strong class='kw'>{m.group(0)}</strong>",
        escaped,
    )


def safe_write_text(path: Path, content: str, encoding: str = "utf-8") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content, encoding=encoding)
        return path
    except PermissionError:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        alt = path.with_name(f"{path.stem}_{ts}{path.suffix}")
        alt.write_text(content, encoding=encoding)
        return alt



# ============================================================
# EMITENTU PAVADINIMU SUVEDIMAS I VIENA FORMA
# ============================================================

LEGAL_FORM_TOKENS = {
    "ab", "uab", "mb", "vsi", "vsi", "as", "asa", "oy", "oyj", "sIA".lower(),
    "akcine", "bendrove", "akcine bendrove", "uzdaroji", "uzdaroji akcine bendrove",
    "public", "limited", "company", "group", "grupe"
}

# Zodziai, kuriu nenorime nukirpti kaip teisiniu formu, nors jie baigiasi panasiomis raidemis.
PROTECTED_WORDS = {"bankas", "bank", "energija", "energies", "grid", "group", "grupe"}


def _remove_lithuanian_accents(s: str) -> str:
    repl = str.maketrans({
        "ą": "a", "č": "c", "ę": "e", "ė": "e", "į": "i", "š": "s", "ų": "u", "ū": "u", "ž": "z",
        "Ą": "A", "Č": "C", "Ę": "E", "Ė": "E", "Į": "I", "Š": "S", "Ų": "U", "Ū": "U", "Ž": "Z",
    })
    return s.translate(repl)


def _issuer_base_key(value) -> str:
    """Grupavimo raktas: AKROPOLIS GROUP UAB ir AKROPOLIS GROUP, UAB -> akropolis."""
    s = norm_text(value).lower()
    s = _remove_lithuanian_accents(s)
    s = s.replace("&quot;", " ").replace("&amp;", " and ")
    s = re.sub(r"[„“”\"'`´,.;:()\[\]{}]", " ", s)
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "unknown"

    tokens = []
    for tok in s.split():
        t = tok.strip("- ")
        if not t:
            continue
        # Teisine forma salinama tik kaip atskiras zodis, ne zodzio dalis.
        if t in PROTECTED_WORDS:
            tokens.append(t)
            continue
        if t in LEGAL_FORM_TOKENS:
            continue
        tokens.append(t)

    key = " ".join(tokens).strip()
    key = re.sub(r"\s+", " ", key)
    return key or s or "unknown"


def _choose_canonical_issuer(values: Iterable[str]) -> str:
    cleaned = [norm_text(v) for v in values if norm_text(v)]
    if not cleaned:
        return "Unknown"
    # Pirmenybe: ilgesnis pavadinimas su lietuviskomis kabutemis/kableliu, bet ne per daug triuksmo.
    def score(v: str):
        has_comma = 1 if "," in v else 0
        has_quotes = 1 if any(ch in v for ch in ['"', '„', '“']) else 0
        upper_penalty = 1 if v.isupper() else 0
        return (has_comma + has_quotes, -upper_penalty, len(v))
    return sorted(cleaned, key=score, reverse=True)[0]


def _canonicalize_issuers(data: pd.DataFrame) -> pd.DataFrame:
    if data is None or data.empty or "issuer" not in data.columns:
        return data
    out = data.copy()
    out["issuer_original"] = out["issuer"].fillna("").astype(str).map(norm_text)
    out["issuer_key"] = out["issuer_original"].map(_issuer_base_key)
    mapping = {}
    for key, grp in out.groupby("issuer_key", dropna=False):
        mapping[key] = _choose_canonical_issuer(grp["issuer_original"].tolist())
    out["issuer"] = out["issuer_key"].map(mapping).fillna(out["issuer_original"])
    out["issuer"] = out["issuer"].replace("", "Unknown")
    return out


# ============================================================
# SUPABASE PAGINATION - KAD NEBUTU 1000 IRASU RIBOS
# ============================================================

REPORT_VERSION = "emitentu_date_raw_first_fix_2026-07-17b"
PAGE_SIZE = 1000


def _date_start_iso(d: date) -> str:
    return f"{d.isoformat()} 00:00:00"


def _date_end_iso(d: date) -> str:
    return f"{d.isoformat()} 23:59:59"


def _load_crib_news_df_paginated(start_date: date, end_date: date, page_size: int = PAGE_SIZE) -> pd.DataFrame:
    """
    Nuskaito visus CRIB irasus is Supabase market_news per kelis puslapius.
    Naudojama, nes standartinis Supabase/PostgREST atsakymas daznai grazina tik 1000 eiluciu.
    """
    if _http_client is None or _supabase_headers is None or _supabase_rest_url is None:
        raise RuntimeError("supabase_cache neturi _http_client / _supabase_headers / _supabase_rest_url helperiu")

    url = _supabase_rest_url("market_news")
    all_rows = []
    offset = 0

    select_cols = "published_at,company,category,title,url,content,source"

    with _http_client() as client:
        while True:
            params = [
                ("select", select_cols),
                ("source", "eq.crib"),
                ("published_at", f"gte.{_date_start_iso(start_date)}"),
                ("published_at", f"lte.{_date_end_iso(end_date)}"),
                ("order", "published_at.asc"),
                ("limit", str(page_size)),
                ("offset", str(offset)),
            ]
            headers = dict(_supabase_headers())
            headers["Range"] = f"{offset}-{offset + page_size - 1}"
            headers["Prefer"] = "count=exact"
            resp = client.get(url, headers=headers, params=params)
            if resp.status_code >= 400:
                raise RuntimeError(f"Supabase market_news nuskaitymo klaida {resp.status_code}: {resp.text[:800]}")
            rows = resp.json() or []
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size

    return pd.DataFrame(all_rows)

# ============================================================
# SUPABASE -> ATASKAITOS DATAFRAME
# ============================================================


def load_crib_news_for_report(start_date: date, end_date: date) -> pd.DataFrame:
    """Ima VISAS CRIB naujienas is Supabase ir suvienodina laukus ataskaitai."""
    columns = [
        "date", "issuer", "type", "category_src", "title", "url", "summary",
        "content", "orig_order", "date_parsed",
    ]

    # Pirmiausia bandome tiesiogini puslapiuota nuskaityma is market_news.
    # Jei aplinkoje nera Supabase REST helperiu, krentame i sena load_news_df varianta.
    pagination_error = None
    try:
        raw = _load_crib_news_df_paginated(start_date, end_date, page_size=PAGE_SIZE)
    except Exception as exc:
        pagination_error = str(exc)
        raw = load_news_df("crib", start_date, end_date)

    if raw is None or raw.empty:
        return pd.DataFrame(columns=columns)

    df = raw.copy()

    def col(name: str, default=""):
        if name in df.columns:
            return df[name]
        return pd.Series([default] * len(df), index=df.index)

    out = pd.DataFrame({
        "date": col("published_at"),
        "issuer": col("company", "Unknown"),
        "type": col("category", ""),
        "category_src": col("category", ""),
        "title": col("title", ""),
        "url": col("url", ""),
        "summary": col("content", ""),
        "content": col("content", ""),
    })

    for c in ["issuer", "type", "category_src", "title", "url", "summary", "content"]:
        out[c] = out[c].fillna("").astype(str).map(norm_text)

    out["issuer"] = out["issuer"].replace("", "Unknown")
    out = _canonicalize_issuers(out)
    out["date_parsed"] = parse_dates_safe(out["date"])
    out = out.sort_values("date_parsed", ascending=True, na_position="last").reset_index(drop=True)
    out["orig_order"] = out.index

    if pagination_error:
        out["_pagination_warning"] = pagination_error

    return out[columns]


def prepare_classified_df(df: pd.DataFrame) -> pd.DataFrame:
    """Klasifikuoja, sutvarko ir pašalina dublikatus."""
    output_columns = [
        "date", "issuer", "type", "category_src", "title", "url", "summary",
        "orig_order", "date_parsed", "matched_categories", "matched_keywords_map",
        "flat_keywords", "combined_text", "news_key",
    ]

    if df is None or df.empty:
        return pd.DataFrame(columns=output_columns)

    data = df.copy().reset_index(drop=True)

    for col in ["date", "issuer", "type", "category_src", "title", "url", "summary"]:
        if col not in data.columns:
            data[col] = ""
        data[col] = data[col].fillna("").astype(str).map(norm_text)

    data["issuer"] = data["issuer"].replace("", "Unknown")
    data = _canonicalize_issuers(data)

    if "date_parsed" not in data.columns:
        data["date_parsed"] = parse_dates_safe(data["date"])
    else:
        data["date_parsed"] = parse_dates_safe(data["date_parsed"])

    if "orig_order" not in data.columns:
        data["orig_order"] = data.index

    data["combined_text"] = (
        data["title"].fillna("") + " " +
        data["summary"].fillna("") + " " +
        data["type"].fillna("") + " " +
        data["category_src"].fillna("")
    ).map(norm_text)

    matched_categories_all = []
    matched_keywords_map_all = []
    flat_keywords_all = []

    for _, row in data.iterrows():
        matched_categories, matched_keywords_map, flat_keywords = find_matches(row.get("combined_text", ""))
        matched_categories_all.append(matched_categories)
        matched_keywords_map_all.append(matched_keywords_map)
        flat_keywords_all.append(flat_keywords)

    data["matched_categories"] = matched_categories_all
    data["matched_keywords_map"] = matched_keywords_map_all
    data["flat_keywords"] = flat_keywords_all

    data["news_key"] = data.apply(_make_news_key, axis=1)

    # Tas pats pranešimas prie to paties emitento rodomas tik vieną kartą.
    # Jei pasitaiko keli įrašai, paliekame tą, kuriame daugiau teksto ir yra URL.
    data["_row_quality"] = (
        data["url"].fillna("").astype(str).str.len().clip(upper=1) * 1000
        + data["summary"].fillna("").astype(str).str.len()
        + data["title"].fillna("").astype(str).str.len()
    )
    data = data.sort_values(["news_key", "_row_quality", "orig_order"], ascending=[True, False, True])
    data = data.drop_duplicates(subset=["news_key"], keep="first")
    data = data.drop(columns=["_row_quality"], errors="ignore")

    data = data.sort_values(["issuer", "date_parsed", "orig_order"], ascending=[True, True, True]).reset_index(drop=True)

    for c in output_columns:
        if c not in data.columns:
            data[c] = ""
    return data[output_columns]


def build_summary_df(df: pd.DataFrame) -> pd.DataFrame:
    category_counts = defaultdict(int)
    if df is not None and not df.empty and "matched_categories" in df.columns:
        for cats in df["matched_categories"]:
            for cat in cats or []:
                category_counts[cat] += 1

    return pd.DataFrame(
        sorted(category_counts.items(), key=lambda x: (-x[1], x[0])),
        columns=["category", "count"],
    )


# ============================================================
# HTML GENERAVIMAS
# ============================================================


def _format_date_for_html(value) -> str:
    dt = _parse_single_date_safe(value)
    if pd.notna(dt):
        return pd.to_datetime(dt).strftime("%Y-%m-%d %H:%M")
    return norm_text(value)


def build_pretty_html(df: pd.DataFrame, title: str = "Klasifikuotos naujienos — pagal emitentus") -> str:
    """Generuoja HTML ataskaitą pagal Jupyter ataskaitos dizainą ir logiką."""
    if df is None or df.empty:
        return f"""
        <!doctype html>
        <html>
        <head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head>
        <body style='font-family:Arial,sans-serif;padding:24px;background:#f6f8fb;color:#111'>
            <h2>{html_lib.escape(title)}</h2>
            <p>Nurodytame laikotarpyje CRIB naujienų duomenų bazėje nerasta.</p>
        </body>
        </html>
        """

    report_df = _canonicalize_issuers(df.copy())
    # Svarbu: HTML generavime datą iš naujo parsinguojame iš pirminio `date` lauko,
    # o ne iš galimai ankstesnėje versijoje klaidingai suformuoto `date_parsed`.
    if "date" in report_df.columns:
        report_df["date_parsed"] = parse_dates_safe(report_df["date"])
    else:
        report_df["date_parsed"] = parse_dates_safe(report_df.get("date_parsed"))
    report_df = report_df.sort_values(["issuer", "date_parsed", "orig_order"], ascending=[True, True, True])

    issuer_order = list(report_df["issuer"].drop_duplicates())
    summary_df = build_summary_df(report_df)

    css = """
    :root{
      --bg:#f6f8fb;
      --card:#ffffff;
      --accent:#0b6ea8;
      --muted:#6b7680;
      --danger:#b30000;
      --line:#e8eef5;
      --soft:#f8fbfe;
      --glass:rgba(11,110,168,0.06);
    }
    html,body{
      min-height:100%;
      margin:0;
      font-family:Inter,Segoe UI,Arial,Helvetica,sans-serif;
      background:var(--bg);
      color:#111;
    }
    .container{
      max-width:1280px;
      margin:28px auto;
      padding:20px;
      box-sizing:border-box;
    }
    header{
      display:flex;
      align-items:center;
      justify-content:space-between;
      padding:12px 0;
      gap:16px;
    }
    header h1{
      margin:0;
      font-size:1.7rem;
      color:var(--accent);
    }
    .meta{
      color:var(--muted);
      font-size:0.95rem;
      margin-top:6px;
    }
    .top{
      display:flex;
      gap:18px;
      align-items:flex-start;
    }
    .toc{
      width:320px;
      min-width:280px;
      background:var(--card);
      padding:14px;
      border-radius:12px;
      box-shadow:0 6px 18px rgba(12,40,60,0.06);
      position:sticky;
      top:16px;
      box-sizing:border-box;
    }
    .toc h3{margin:0 0 8px 0;}
    .toc input[type="text"]{
      width:100%;
      padding:8px;
      border-radius:8px;
      border:1px solid #e7eef6;
      box-sizing:border-box;
    }
    .toc ul{
      list-style:none;
      padding:8px 0;
      margin:10px 0 0 0;
      max-height:70vh;
      overflow:auto;
    }
    .toc li{margin:6px 0;}
    .toc a{
      display:flex;
      justify-content:space-between;
      text-decoration:none;
      color:#0b4860;
      padding:6px 8px;
      border-radius:8px;
      gap:8px;
      overflow-wrap:anywhere;
      word-break:break-word;
    }
    .toc a:hover{background:var(--glass);}
    .content{
      flex:1;
      margin-left:8px;
      min-width:0;
      max-width:100%;
    }
    .summary-card{
      background:linear-gradient(180deg,#fff,#fbfdff);
      padding:14px;
      border-radius:12px;
      box-shadow:0 6px 20px rgba(12,40,60,0.04);
      margin-bottom:14px;
      line-height:1.5;
      overflow-wrap:anywhere;
      word-break:break-word;
      box-sizing:border-box;
    }
    .issuer-card{
      background:var(--card);
      padding:14px;
      margin-bottom:14px;
      border-radius:12px;
      box-shadow:0 6px 18px rgba(12,40,60,0.04);
      overflow:hidden;
      box-sizing:border-box;
    }
    .issuer-header{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:12px;
      padding-bottom:8px;
      border-bottom:1px solid var(--line);
    }
    .issuer-title{
      font-size:1.18rem;
      font-weight:800;
      color:#083b50;
      overflow-wrap:anywhere;
      word-break:break-word;
    }
    .badge{
      background:var(--accent);
      color:white;
      padding:6px 10px;
      border-radius:999px;
      font-weight:600;
      font-size:0.85rem;
      white-space:nowrap;
    }
    .small{font-size:0.85rem;color:var(--muted);}
    .controls{display:flex;gap:8px;align-items:center;flex-shrink:0;}
    .btn{
      background:var(--accent);
      color:white;
      padding:8px 10px;
      border-radius:8px;
      text-decoration:none;
      font-weight:600;
      cursor:pointer;
      border:none;
    }
    .toggle-btn{
      background:#f3f7fb;
      border-radius:8px;
      padding:6px 8px;
      border:1px solid #e7eef6;
      cursor:pointer;
    }
    .collapsible{overflow:hidden;transition:max-height .25s ease-out;}
    table{
      width:100%;
      table-layout:fixed;
      border-collapse:collapse;
      margin-top:12px;
    }
    th,td{
      padding:10px 8px;
      border-bottom:1px solid #eef6fb;
      text-align:left;
      vertical-align:top;
      overflow-wrap:anywhere;
      word-break:break-word;
      box-sizing:border-box;
    }
    th{
      background:#fbfdff;
      font-weight:700;
      color:#234;
      position:sticky;
      top:0;
      z-index:1;
    }
    .meta-box{
      margin-top:6px;
      display:flex;
      flex-wrap:wrap;
      gap:6px;
    }
    .pill{
      display:inline-block;
      background:#eef6fb;
      color:#184b66;
      padding:4px 8px;
      border-radius:999px;
      font-size:0.8rem;
      line-height:1.2;
      max-width:100%;
      overflow-wrap:anywhere;
      word-break:break-word;
    }
    .pill-key{background:#fff3f3;color:#8f1f1f;}
    .title-link{
      font-weight:700;
      color:#0b5575;
      text-decoration:none;
      overflow-wrap:anywhere;
      word-break:break-word;
    }
    .title-link:hover{text-decoration:underline;}
    .summary-text{
      line-height:1.5;
      white-space:normal;
      overflow-wrap:anywhere;
      word-break:break-word;
      max-width:100%;
    }
    strong.kw{
      font-weight:800;
      color:var(--danger);
      background:rgba(179,0,0,0.06);
      padding:0 1px;
      border-radius:3px;
    }
    .muted{color:var(--muted);}
    @media screen and (max-width:900px){
      .top{flex-direction:column;}
      .toc{width:100%;min-width:0;position:static;}
      .content{width:100%;margin-left:0;}
      header{flex-direction:column;align-items:flex-start;}
    }
    @media print{
      .toc,.controls{display:none;}
      .container{max-width:100%;margin:0;padding:0;}
      .top{display:block;}
      .content{margin-left:0;}
      .collapsible{max-height:none!important;overflow:visible!important;}
      table{table-layout:fixed;}
    }
    """

    js = """
    function scrollToId(id){
      const el = document.querySelector('#' + id);
      if(el){el.scrollIntoView({behavior:'smooth', block:'start'});}
    }
    function expandAll(){
      document.querySelectorAll('.collapsible').forEach(div => {
        div.style.maxHeight = 'none';
        div.style.overflow = 'visible';
      });
      document.querySelectorAll('[data-toggle]').forEach(btn => {btn.innerText = 'Slėpti';});
      const g = document.getElementById('global_toggle');
      if(g){g.innerText = 'Slėpti visus';}
    }
    function collapseAll(){
      document.querySelectorAll('.collapsible').forEach(div => {
        div.style.maxHeight = '0px';
        div.style.overflow = 'hidden';
      });
      document.querySelectorAll('[data-toggle]').forEach(btn => {btn.innerText = 'Rodyti';});
      const g = document.getElementById('global_toggle');
      if(g){g.innerText = 'Rodyti visus';}
    }
    function toggleAll(){
      let anyClosed = false;
      document.querySelectorAll('.collapsible').forEach(div => {
        if(!div.style.maxHeight || div.style.maxHeight === '0px'){anyClosed = true;}
      });
      if(anyClosed){expandAll();} else {collapseAll();}
    }
    document.addEventListener('click', function(e){
      if(e.target.matches('[data-toggle]') || e.target.closest('[data-toggle]')){
        const btn = e.target.closest('[data-toggle]');
        const target = document.querySelector(btn.dataset.toggle);
        if(!target){return;}
        if(target.style.maxHeight && target.style.maxHeight !== '0px'){
          target.style.maxHeight = '0px';
          target.style.overflow = 'hidden';
          btn.innerText = 'Rodyti';
        } else {
          target.style.maxHeight = 'none';
          target.style.overflow = 'visible';
          btn.innerText = 'Slėpti';
        }
      }
    });
    function tocFilter(){
      const q = document.getElementById('toc_search').value.trim().toLowerCase();
      document.querySelectorAll('.toc li').forEach(li => {
        const txt = li.dataset.issuer || '';
        li.style.display = txt.indexOf(q) !== -1 ? '' : 'none';
      });
    }
    document.addEventListener('DOMContentLoaded', function(){
      document.querySelectorAll('.collapsible').forEach(div => {
        div.style.maxHeight = '0px';
        div.style.overflow = 'hidden';
      });
      const g = document.getElementById('global_toggle');
      if(g){g.innerText = 'Rodyti visus';}
    });
    """

    parts = []
    parts.append("<!doctype html>")
    parts.append("<html>")
    parts.append("<head>")
    parts.append("<meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append(f"<title>{html_lib.escape(title)}</title>")
    parts.append(f"<style>{css}</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append("<div class='container'>")

    parts.append("<header>")
    parts.append("<div style='display:flex;flex-direction:column'>")
    parts.append(f"<h1>{html_lib.escape(title)}</h1>")
    parts.append(f"<div class='meta'>Generuota: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Versija: {REPORT_VERSION}</div>")
    parts.append("</div>")
    parts.append("<div class='controls'>")
    parts.append("<button id='global_toggle' class='btn' onclick='toggleAll()'>Rodyti visus</button>")
    parts.append("</div>")
    parts.append("</header>")

    parts.append("<div class='top'>")

    parts.append("<aside class='toc'>")
    parts.append("<h3>Emitentų sąrašas</h3>")
    parts.append("<input id='toc_search' placeholder='Ieškoti emitento...' oninput='tocFilter()' />")
    parts.append("<ul>")
    for issuer in issuer_order:
        safe = slugify(issuer)
        cnt = int((report_df["issuer"] == issuer).sum())
        parts.append(
            f"<li data-issuer='{html_lib.escape(str(issuer).lower())}'>"
            f"<a href='javascript:void(0)' onclick=\"scrollToId('{safe}')\">"
            f"{html_lib.escape(str(issuer))}"
            f"<span class='badge'>{cnt}</span>"
            f"</a></li>"
        )
    parts.append("</ul>")
    parts.append("</aside>")

    parts.append("<main class='content'>")

    if not summary_df.empty:
        cat_summary_str = ", ".join(
            f"{html_lib.escape(str(r['category']))}: {int(r['count'])}"
            for _, r in summary_df.iterrows()
        )
    else:
        cat_summary_str = "Nėra aptiktų temų"

    parts.append(
        "<div class='summary-card'>"
        "<div><strong>Santrauka</strong></div>"
        f"<div>Visų įrašų skaičius: <strong>{len(report_df)}</strong></div>"
        f"<div>Emitentų skaičius: <strong>{report_df['issuer'].nunique()}</strong></div>"
        f"<div>Temų pasiskirstymas pagal aptikimus: {cat_summary_str}</div>"
        "<div class='small' style='margin-top:8px'>"
        "Pastaba: kiekvienas pranešimas prie emitento rodomas tik vieną kartą. "
        "Temos ir raktažodžiai pateikiami informaciniam kontekstui, bet nebeskaido įrašo į kelias sekcijas."
        "</div>"
        "</div>"
    )

    for issuer in issuer_order:
        issuer_rows = report_df[report_df["issuer"] == issuer].copy()
        issuer_rows = issuer_rows.sort_values(["date_parsed", "orig_order"], ascending=[True, True], na_position="last")
        safe = slugify(issuer)

        parts.append(f"<section id='{safe}' class='issuer-card'>")
        parts.append("<div class='issuer-header'>")
        parts.append(
            "<div>"
            f"<div class='issuer-title'>{html_lib.escape(str(issuer))}</div>"
            f"<div class='small'>{len(issuer_rows)} įrašai</div>"
            "</div>"
        )
        parts.append(
            f"<div class='controls'>"
            f"<button class='toggle-btn' data-toggle='#ct_{safe}'>Rodyti</button>"
            f"</div>"
        )
        parts.append("</div>")

        parts.append(f"<div id='ct_{safe}' class='collapsible'>")
        parts.append("<table>")
        parts.append(
            "<thead><tr>"
            "<th style='width:140px'>Data</th>"
            "<th style='width:36%'>Antraštė / temos</th>"
            "<th>Santrauka</th>"
            "</tr></thead>"
        )
        parts.append("<tbody>")

        for _, r in issuer_rows.iterrows():
            # Rodome datą pagal pirminį DB lauką `date`, nes jis ateina iš CRIB/Supabase teisingu formatu.
            # `date_parsed` naudojamas tik rikiavimui.
            date_raw = _format_date_for_html(r.get("date", "")) or _format_date_for_html(r.get("date_parsed"))
            title_raw = norm_text(r.get("title", ""))
            url = norm_text(r.get("url", ""))
            summary_raw = norm_text(r.get("summary", ""))
            matched_categories = r.get("matched_categories") or []
            matched_keywords = r.get("flat_keywords") or []

            title_html = highlight_keywords(title_raw, matched_keywords)
            summary_html = highlight_keywords(summary_raw, matched_keywords)

            if url:
                url_escaped = html_lib.escape(url)
                link_text = title_html or url_escaped
                link_html = (
                    f"<a class='title-link' href='{url_escaped}' "
                    f"target='_blank' rel='noreferrer'>{link_text}</a>"
                )
            else:
                link_html = f"<span class='title-link'>{title_html}</span>"

            meta_bits = []
            for cat in matched_categories:
                meta_bits.append(f"<span class='pill'>{html_lib.escape(str(cat))}</span>")
            for kw in matched_keywords:
                meta_bits.append(f"<span class='pill pill-key'>{html_lib.escape(str(kw))}</span>")

            meta_html = f"<div class='meta-box'>{''.join(meta_bits)}</div>" if meta_bits else ""

            parts.append(
                "<tr>"
                f"<td>{html_lib.escape(str(date_raw))}</td>"
                f"<td>{link_html}{meta_html}</td>"
                f"<td><div class='summary-text'>{summary_html}</div></td>"
                "</tr>"
            )

        parts.append("</tbody>")
        parts.append("</table>")
        parts.append("</div>")
        parts.append("</section>")

    parts.append("</main>")
    parts.append("</div>")
    parts.append("</div>")
    parts.append(f"<script>{js}</script>")
    parts.append("</body>")
    parts.append("</html>")

    return "\n".join(parts)


# ============================================================
# VIEŠOS FUNKCIJOS STREAMLIT INTEGRACIJAI
# ============================================================


def generate_emitentu_ataskaita(start_date: date, end_date: date, title: Optional[str] = None) -> dict:
    """
    Sugeneruoja emitentų atrankos HTML ataskaitą iš Supabase CRIB naujienų.

    Grąžina dict:
    - html
    - df
    - summary
    - start_date
    - end_date
    """
    if start_date is None or end_date is None:
        raise ValueError("Reikia nurodyti start_date ir end_date.")
    if start_date > end_date:
        raise ValueError("Data 'Nuo' negali būti vėlesnė už datą 'Iki'.")

    raw_df = load_crib_news_for_report(start_date, end_date)
    df = prepare_classified_df(raw_df)

    if title is None:
        title = f"Klasifikuotos naujienos — pagal emitentus ({start_date} – {end_date})"

    html = build_pretty_html(df, title=title)
    summary = build_summary_df(df)

    return {
        "html": html,
        "df": df,
        "summary": summary,
        "start_date": start_date,
        "end_date": end_date,
    }


def save_emitentu_ataskaita_html(start_date: date, end_date: date, output_path) -> Path:
    """Sugeneruoja ir išsaugo HTML failą lokaliai."""
    result = generate_emitentu_ataskaita(start_date, end_date)
    return safe_write_text(Path(output_path), result["html"], encoding="utf-8")


def render_emitentu_atranka_page(default_days: int = 30):
    """Streamlit puslapis, kviečiamas iš app.py."""
    import streamlit as st
    import streamlit.components.v1 as components

    st.subheader("🧾 Emitentų atranka pagal CRIB naujienas")
    st.caption(
        "Duomenys imami iš Supabase market_news lentelės, source='crib'. "
        "Nauja versija nuskaito visus įrašus per puslapius, todėl nebeturi 1000 eilučių ribos. "
        f"Versija: {REPORT_VERSION}."
    )

    today = date.today()
    col1, col2 = st.columns(2)
    with col1:
        start = st.date_input("Nuo", today - timedelta(days=default_days), key="crib_cls_start")
    with col2:
        end = st.date_input("Iki", today, key="crib_cls_end")

    if start > end:
        st.error("Data 'Nuo' negali būti vėlesnė už datą 'Iki'.")
        return

    generate_clicked = st.button("Generuoti emitentų ataskaitą", key="crib_cls_generate", use_container_width=True)

    if not generate_clicked:
        st.info("Pasirink laikotarpį ir spausk „Generuoti emitentų ataskaitą“.")
        return

    with st.spinner("Kraunamos CRIB naujienos iš duomenų bazės ir generuojama HTML ataskaita..."):
        result = generate_emitentu_ataskaita(start, end)

    st.success(f"Rasta įrašų po dublikatų ir emitentų suvienodinimo: {len(result['df'])}")
    st.caption("Jeigu laikotarpyje yra daugiau nei 1000 CRIB įrašų, ši versija juos nuskaito per kelis Supabase puslapius.")

    if not result["summary"].empty:
        st.markdown("**Temų aptikimo santrauka**")
        st.dataframe(result["summary"], use_container_width=True, hide_index=True)
    else:
        st.info("Pagal nustatytus raktažodžius temų neaptikta, bet naujienos vis tiek rodomos pagal emitentus.")

    st.download_button(
        "Atsisiųsti HTML ataskaitą",
        data=result["html"].encode("utf-8"),
        file_name=f"emitentu_atranka_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.html",
        mime="text/html",
        use_container_width=True,
    )

    components.html(result["html"], height=900, scrolling=True)


# Suderinamumo alias, jei app.py kada nors kvies kitaip.
show_emitentu_atranka_page = render_emitentu_atranka_page


if __name__ == "__main__":
    res = generate_emitentu_ataskaita(date.today() - timedelta(days=30), date.today())
    out = Path(f"emitentu_atranka_{date.today().strftime('%Y%m%d')}.html")
    safe_write_text(out, res["html"], encoding="utf-8")
    print(f"Išsaugota: {out.resolve()}")
    if not res["summary"].empty:
        print(res["summary"].to_string(index=False))
    else:
        print("Nėra aptiktų temų.")

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


EMITENTU_ATRANKA_VERSION = "emitentu_suvienodinimas_2026-07-15c"


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


def parse_dates_safe(series: pd.Series) -> pd.Series:
    """Saugus datų parsavimas iš kelių galimų formatų."""
    if series is None:
        return pd.Series(dtype="datetime64[ns]")

    s = series.copy()
    parsed = pd.to_datetime(s, format="%Y-%m-%d %H:%M:%S", errors="coerce")

    if parsed.isna().any():
        idx = parsed[parsed.isna()].index
        if len(idx) > 0:
            parsed_alt = pd.to_datetime(s.loc[idx], dayfirst=True, errors="coerce")
            parsed.loc[parsed_alt.index] = parsed_alt

    if parsed.isna().any():
        idx = parsed[parsed.isna()].index
        if len(idx) > 0:
            parsed_alt = pd.to_datetime(s.loc[idx], errors="coerce")
            parsed.loc[parsed_alt.index] = parsed_alt

    return parsed


def _norm_for_dedup(value) -> str:
    s = norm_text(value).lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip()


_LT_TRANSLATION = str.maketrans({
    "ą": "a", "č": "c", "ę": "e", "ė": "e", "į": "i",
    "š": "s", "ų": "u", "ū": "u", "ž": "z",
    "Ą": "a", "Č": "c", "Ę": "e", "Ė": "e", "Į": "i",
    "Š": "s", "Ų": "u", "Ū": "u", "Ž": "z",
})

_LEGAL_FORM_PATTERNS = [
    r"uzdaroji\s+akcine\s+bendrove",
    r"uždaroji\s+akcinė\s+bendrovė",
    r"akcine\s+bendrove",
    r"akcinė\s+bendrovė",
    r"\buab\b",
    r"\bab\b",
    r"\bas\b",
    r"\basa\b",
    r"\boy\b",
    r"\bsia\b",
]

_LEGAL_SUFFIX_RE = re.compile(
    # Teisine forma turi buti atskiras zodis gale.
    # Svarbu: neturi sutapti su zodziais kaip "bankas", kurie baigiasi "as".
    r"(?:\s*,\s*|\s+)(UAB|AB|AS|ASA|OY|SIA)\s*$",
    flags=re.I | re.U,
)

_LEGAL_PREFIX_RE = re.compile(
    # Teisine forma turi buti atskiras zodis pradzioje.
    r"^\s*(UAB|AB|AS|ASA|OY|SIA)\s+",
    flags=re.I | re.U,
)


def _issuer_base_key(value) -> str:
    """Sukuria emitento grupavimo raktą.

    Pvz. "AKROPOLIS GROUP UAB" ir "AKROPOLIS GROUP, UAB" abu tampa
    "akropolis group". Taip ataskaitoje neatsiranda dviejų blokų dėl
    kablelio, teisinės formos ar raidžių registro skirtumų.
    """
    s = norm_text(value)
    if not s:
        return ""

    s = s.translate(_LT_TRANSLATION).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[„“\"'`´]", "", s)
    s = re.sub(r"[\.,;:()\[\]{}]+", " ", s)

    for pattern in _LEGAL_FORM_PATTERNS:
        s = re.sub(pattern, " ", s, flags=re.I | re.U)

    s = re.sub(r"\bgroup\s+group\b", "group", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _clean_issuer_display(value) -> str:
    """Sutvarko pavadinimą rodymui, bet nepakeičia jo esmės."""
    s = norm_text(value)
    if not s or s.lower() == "unknown":
        return "Unknown"

    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*", ", ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _issuer_display_score(value) -> int:
    """Parenka gražiausią pavadinimo variantą iš kelių to paties emitento formų."""
    s = _clean_issuer_display(value)
    if not s or s == "Unknown":
        return -10000

    score = 0
    if re.search(r",\s*(UAB|AB|AS|ASA|OY|SIA)\s*$", s, flags=re.I):
        score += 100
    if re.search(r"^(AB|UAB|AS|ASA|OY|SIA)\s+", s, flags=re.I):
        score += 60
    if s.isupper():
        score -= 5
    score += min(len(s), 80)
    return score


def _canonical_issuer_from_values(values) -> str:
    clean = [_clean_issuer_display(v) for v in values if _clean_issuer_display(v) != "Unknown"]
    if not clean:
        return "Unknown"

    best = sorted(clean, key=lambda x: (_issuer_display_score(x), x), reverse=True)[0]

    # Jei pavadinimas baigiasi teisine forma be kablelio, rodome su kableliu:
    # "AKROPOLIS GROUP UAB" -> "AKROPOLIS GROUP, UAB".
    m = _LEGAL_SUFFIX_RE.search(best)
    if m:
        suffix = m.group(1).upper()
        base = _LEGAL_SUFFIX_RE.sub("", best).strip(" ,")
        if base:
            return f"{base}, {suffix}"

    # Jei teisinė forma yra priekyje, paliekame įprastą AB ... formą.
    m = _LEGAL_PREFIX_RE.search(best)
    if m:
        prefix = m.group(1).upper()
        rest = _LEGAL_PREFIX_RE.sub("", best).strip()
        if rest:
            return f"{prefix} {rest}"

    return best


def _canonicalize_issuers(data: pd.DataFrame) -> pd.DataFrame:
    """Suvienodina emitentų pavadinimus prieš grupavimą ir dublikatų šalinimą."""
    if data is None or data.empty or "issuer" not in data.columns:
        return data

    out = data.copy()
    out["issuer"] = out["issuer"].fillna("Unknown").astype(str).map(_clean_issuer_display)
    out["issuer_key"] = out["issuer"].map(_issuer_base_key)
    out.loc[out["issuer_key"].eq(""), "issuer_key"] = out.loc[out["issuer_key"].eq(""), "issuer"].map(_norm_for_dedup)

    canonical_by_key = {}
    for key, group in out.groupby("issuer_key", dropna=False):
        canonical_by_key[key] = _canonical_issuer_from_values(group["issuer"].tolist())

    out["issuer"] = out["issuer_key"].map(canonical_by_key).fillna(out["issuer"])
    return out.drop(columns=["issuer_key"], errors="ignore")


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
# SUPABASE -> ATASKAITOS DATAFRAME
# ============================================================


def load_crib_news_for_report(start_date: date, end_date: date) -> pd.DataFrame:
    """Ima CRIB naujienas iš Supabase ir suvienodina laukus ataskaitai."""
    raw = load_news_df("crib", start_date, end_date)

    columns = [
        "date", "issuer", "type", "category_src", "title", "url", "summary",
        "content", "orig_order", "date_parsed",
    ]

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
    out["date_parsed"] = parse_dates_safe(out["date"])
    out = out.sort_values("date_parsed", ascending=True, na_position="last").reset_index(drop=True)
    out["orig_order"] = out.index

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
        data["date_parsed"] = pd.to_datetime(data["date_parsed"], errors="coerce")

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
    try:
        dt = pd.to_datetime(value, errors="coerce")
        if pd.notna(dt):
            return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
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
    report_df["date_parsed"] = pd.to_datetime(report_df.get("date_parsed"), errors="coerce")
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
    parts.append(f"<div class='meta'>Generuota: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>")
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
        f"<div>Versija: <strong>{EMITENTU_ATRANKA_VERSION}</strong></div>"
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
            date_raw = _format_date_for_html(r.get("date_parsed")) or _format_date_for_html(r.get("date", ""))
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
        "Ataskaita generuojama pagal emitentus, be kategorijų blokų, su aptiktomis temomis ir raktažodžiais. "
        "Versija: emitentu_suvienodinimas_2026-07-15c."
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

    st.success(f"Rasta įrašų: {len(result['df'])}")

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

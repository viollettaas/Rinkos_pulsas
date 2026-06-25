import re
import html as html_lib
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from supabase_cache import load_news_df


# ============================================================
# KATEGORIJOS IR RAKTAZODZIAI
# ============================================================

_LIT_ENDINGS = [
    "as", "is", "us", "ys", "a", "o", "ui", "ų", "e", "ei", "em", "ame", "ais",
    "ose", "oje", "os", "om", "ųją", "ai", "iu", "čia", "čią", "",
]


def inflect_pattern(stem: str) -> str:
    if any(ch in stem for ch in r".^$*+?{}[]\|()"):
        return stem

    esc_stem = re.escape(stem)
    nonempty = [re.escape(e) for e in sorted(set(_LIT_ENDINGS)) if e]
    group = "|".join(nonempty)

    if group:
        return rf"\b{esc_stem}(?:{group})?\b"

    return rf"\b{esc_stem}\b"


RAW_CATEGORIES = {
    "Teismo_procesai": [
        "teism", "byl", "ieškin", "skund", "apeliac", "nutartis", "sprendim", "priteis",
        r"teism(?:o|ui|e|ą).*ieškin", r"prašymas.*teism", r"civilin(?:ė|is).*byl",
    ],
    "Verslo_jungimai_ir_pardavimai": [
        "įsigij", "įsigy", "pirk", "pardav", "sandor", "susijung", "perleid", "kontrolin",
        "akcijų pirk", r"\b(spin[- ]?off|M&A|merger|acquisit)\b", r"perleidim.*akcij",
        r"pardavim.*versl",
    ],
    "Restruktūrizavimas": [
        "restruktūriz", "refinans", "bankrot", "likvidav", "skol",
        r"restruktūrizavimo plan", r"creditor.*agreement",
    ],
    "Finansiniai_signalai": [
        "nuostol", "grynasis peln", "peln", "EBITDA", "EBIT", "pajam", "sumažėj",
        "rekord", "smuk", "nuosmuk", "write[- ]?off", "impairment", "likvidum", "profit warning",
        r"prognoz.*(sumaž|sumažėj)", r"rezultat.*pablog",
    ],
    "Apskaitos_politikos_keitimas": [
        "apskaitos politika", "apskaitos pakeit", "perklasifikav", "restatement", "koregavim",
        "finansin.*koreg", r"ankstesni.*koregavim",
    ],
    "Valdymo_pokyciai": [
        "generalin", "direktori", "finansų direktori", "CEO", "CFO", "vadov",
        "atsistatydin", "atleid", "paskirt", "valdyb", "stebėtoj", "laikinas vadov", r"prieš kadenc",
    ],
    "Audito_nutraukimas": [
        "audito sutart", "audito įmon", "auditor", "nutrauk", "atsisak",
        "neigiama auditoriaus", "sąlyginė nuomon", r"audito pakeit",
    ],
    "Kiti": [
        "dividend", "akcijų išpirk", "emisij", "obligacij", "kredito linij", "garantij", "covenant",
    ],
}


COMPILED = {}

for cat, tokens in RAW_CATEGORIES.items():
    compiled_list = []

    for token in tokens:
        if " " in token or any(ch in token for ch in r".^$*+?{}[]\|()"):
            try:
                compiled_list.append(re.compile(token, flags=re.I | re.U))
            except re.error:
                compiled_list.append(re.compile(re.escape(token), flags=re.I | re.U))
        else:
            compiled_list.append(re.compile(inflect_pattern(token), flags=re.I | re.U))

    COMPILED[cat] = compiled_list


CATEGORY_ORDER = list(RAW_CATEGORIES.keys())


# ============================================================
# PAGALBINĖS FUNKCIJOS
# ============================================================

def norm_text(s) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def slugify(s) -> str:
    s = str(s).strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^\w\-_\.]", "", s)
    return s[:120] or "unknown"


def parse_dates_safe(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, format="%Y-%m-%d %H:%M:%S", errors="coerce")

    if parsed.isna().any():
        rem_idx = parsed[parsed.isna()].index
        if not rem_idx.empty:
            remainder = series.loc[rem_idx]
            parsed_remainder = pd.to_datetime(remainder, dayfirst=True, errors="coerce")
            parsed.loc[parsed_remainder.index] = parsed_remainder

    if parsed.isna().any():
        rem_idx = parsed[parsed.isna()].index
        if not rem_idx.empty:
            remainder = series.loc[rem_idx]
            parsed_remainder = pd.to_datetime(remainder, errors="coerce")
            parsed.loc[parsed_remainder.index] = parsed_remainder

    return parsed


def score_categories(text: str) -> dict:
    scores = defaultdict(int)

    for cat, pats in COMPILED.items():
        for pat in pats:
            if pat.search(text or ""):
                scores[cat] += 1

    return scores


def classify_row_text(text: str, allow_multiple: bool = True):
    scores = score_categories(text)

    if not scores:
        return ["Kiti"]

    max_score = max(scores.values()) if scores else 0

    if max_score == 0:
        return ["Kiti"]

    winners = [cat for cat, sc in scores.items() if sc == max_score and sc > 0]

    return winners if allow_multiple else (winners[:1] if winners else ["Kiti"])


def highlight_keywords(text: str) -> str:
    if not text:
        return ""

    escaped = html_lib.escape(str(text))

    for pats in COMPILED.values():
        for pat in pats:
            try:
                escaped = pat.sub(
                    lambda m: f"<strong class='kw'>{m.group(0)}</strong>",
                    escaped,
                )
            except re.error:
                continue

    return escaped


def find_matched_keywords(text: str) -> str:
    if not text:
        return ""

    found = []

    for _, pats in COMPILED.items():
        for pat in pats:
            for m in pat.finditer(text):
                val = norm_text(m.group(0))
                existing = [x.lower() for x in found]

                if val and val.lower() not in existing:
                    found.append(val)

    return ", ".join(found[:12])


# ============================================================
# SUPABASE -> ATASKAITOS DATAFRAME
# ============================================================

def load_crib_news_for_report(start_date: date, end_date: date) -> pd.DataFrame:
    raw = load_news_df("crib", start_date, end_date)

    if raw is None or raw.empty:
        return pd.DataFrame(columns=[
            "date", "issuer", "type", "category_src", "title", "url", "summary", "orig_order",
        ])

    df = raw.copy()

    def col(name, default=""):
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
    })

    out["issuer"] = out["issuer"].fillna("Unknown").astype(str).replace({"": "Unknown"})
    out["title"] = out["title"].fillna("").astype(str)
    out["url"] = out["url"].fillna("").astype(str)
    out["summary"] = out["summary"].fillna("").astype(str)
    out["type"] = out["type"].fillna("").astype(str)
    out["category_src"] = out["category_src"].fillna("").astype(str)

    out["date_parsed"] = parse_dates_safe(out["date"])
    out = out.sort_values("date_parsed", ascending=False).reset_index(drop=True)
    out["orig_order"] = out.index

    return out


def prepare_classified_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=[
            "date", "issuer", "type", "category_src", "title", "url", "summary",
            "orig_order", "combined_text", "categories", "categories_str", "matched_keywords",
        ])

    df = df.copy().reset_index(drop=True)

    df["title"] = df["title"].fillna("").astype(str)
    df["summary"] = df["summary"].fillna("").astype(str)
    df["type"] = df.get("type", "").fillna("").astype(str)
    df["issuer"] = df["issuer"].fillna("Unknown").astype(str)
    df["category_src"] = df.get("category_src", "").fillna("").astype(str)

    if "orig_order" not in df.columns:
        df["orig_order"] = df.index

    df["combined_text"] = (
        df["title"] + " " + df["summary"] + " " + df["type"] + " " + df["issuer"]
    ).str.lower()

    assigned = []
    matched = []

    for _, row in df.iterrows():
        text = (
            row["title"] + " " +
            row["summary"] + " " +
            row["type"] + " " +
            row["issuer"]
        ).lower()

        cats = classify_row_text(text, allow_multiple=True)

        assigned.append(cats)
        matched.append(find_matched_keywords(text))

    df["categories"] = assigned
    df["categories_str"] = df["categories"].apply(
        lambda x: ";".join(x) if isinstance(x, (list, tuple)) else str(x)
    )
    df["matched_keywords"] = matched

    return df


# ============================================================
# HTML GENERAVIMAS
# ============================================================


def build_pretty_html(
    df: pd.DataFrame,
    title: str = "Emitentų atranka pagal CRIB naujienas",
) -> str:
    """Moderni pilno plocio HTML ataskaita be vidiniu nepatogiu slankikliu."""
    if df is None or df.empty:
        return f"""
        <!doctype html>
        <html>
        <head><meta charset='utf-8'></head>
        <body style='font-family:Arial,sans-serif;padding:24px;background:#f6f8fb;color:#111827'>
            <h2>{html_lib.escape(title)}</h2>
            <p>Nurodytame laikotarpyje CRIB naujienų duomenų bazėje nerasta.</p>
        </body>
        </html>
        """

    import json

    df_tmp = df.copy().reset_index(drop=True)
    if "date_parsed" not in df_tmp.columns:
        df_tmp["date_parsed"] = parse_dates_safe(df_tmp["date"])

    df_tmp["issuer"] = df_tmp["issuer"].fillna("Unknown").astype(str).replace({"": "Unknown"})
    df_tmp["title"] = df_tmp["title"].fillna("").astype(str)
    df_tmp["summary"] = df_tmp["summary"].fillna("").astype(str)
    df_tmp["type"] = df_tmp.get("type", "").fillna("").astype(str)
    df_tmp["matched_keywords"] = df_tmp.get("matched_keywords", "").fillna("").astype(str)

    df_tmp = df_tmp.sort_values(
        by=["date_parsed", "orig_order"],
        ascending=[False, True],
    ).reset_index(drop=True)

    def row_categories(row) -> list[str]:
        cats = []
        raw = row.get("categories", [])
        if isinstance(raw, (list, tuple, set)):
            cats.extend([str(x).strip() for x in raw if str(x).strip()])
        elif raw:
            cats.extend([x.strip() for x in str(raw).replace(";", ",").split(",") if x.strip()])
        src = str(row.get("category_src", "") or "").strip()
        if src:
            cats.append(src)
        out = []
        seen = set()
        for c in cats or ["Kiti"]:
            if c.lower() not in seen:
                seen.add(c.lower())
                out.append(c)
        return out or ["Kiti"]

    issuers = sorted(df_tmp["issuer"].dropna().astype(str).unique().tolist())
    all_categories = []
    seen_categories = set()
    for _, r in df_tmp.iterrows():
        for cat in row_categories(r):
            key = cat.lower()
            if key not in seen_categories:
                seen_categories.add(key)
                all_categories.append(cat)
    all_categories = sorted(all_categories, key=lambda x: x.lower())

    rows_json = []
    for i, r in df_tmp.iterrows():
        cats = row_categories(r)
        combined = " ".join([
            str(r.get("issuer", "")),
            str(r.get("title", "")),
            str(r.get("summary", "")),
            str(r.get("type", "")),
            str(r.get("matched_keywords", "")),
            " ".join(cats),
        ])
        rows_json.append({
            "id": f"row_{i}",
            "issuer": str(r.get("issuer", "Unknown") or "Unknown"),
            "cats": cats,
            "text": combined,
        })

    data_json = json.dumps(rows_json, ensure_ascii=False)

    css = """
    :root{
      --bg:#f5f7fb;--panel:#ffffff;--text:#111827;--muted:#64748b;--line:#e5edf6;
      --blue:#075985;--blue2:#0f75bc;--soft:#eef7ff;--red:#c1121f;--amber:#fff7ed;
      --green:#047857;--shadow:0 14px 35px rgba(15,23,42,.08);--radius:18px;
    }
    *{box-sizing:border-box}
    html,body{margin:0;min-height:100%;font-family:Inter,Segoe UI,Arial,Helvetica,sans-serif;background:var(--bg);color:var(--text);overflow-x:hidden}
    a{color:#075985;text-decoration:none} a:hover{text-decoration:underline}
    .app{width:100%;max-width:none;margin:0;padding:14px 14px 28px}
    .hero{background:linear-gradient(135deg,#ffffff 0%,#eff8ff 56%,#dceeff 100%);border:1px solid #d7e8f7;border-radius:22px;padding:18px 20px;box-shadow:var(--shadow);display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:12px}
    .hero h1{margin:0;color:#06243d;font-size:26px;line-height:1.15}.hero .meta{margin-top:8px;color:var(--muted);font-size:13px;line-height:1.4}.stats{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}.stat{background:#fff;border:1px solid var(--line);border-radius:15px;padding:10px 14px;min-width:105px}.stat b{font-size:22px;color:#06243d}.stat span{display:block;color:var(--muted);font-size:12px;margin-top:2px}
    .toolbar{position:sticky;top:0;z-index:20;background:rgba(245,247,251,.96);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:18px;box-shadow:0 10px 28px rgba(15,23,42,.07);padding:12px;margin-bottom:12px}
    .toolbar-grid{display:grid;grid-template-columns:minmax(320px,1.6fr) minmax(210px,.8fr) minmax(210px,.8fr) auto;gap:10px;align-items:end}.field label{display:block;font-size:12px;color:#475569;font-weight:800;margin:0 0 5px 2px}.field input,.field select{width:100%;height:42px;border:1px solid #cfddeb;background:#fff;border-radius:12px;padding:0 12px;color:#0f172a;font-size:14px}.buttons{display:flex;gap:8px;flex-wrap:wrap}.btn{height:42px;border:0;border-radius:12px;background:#075985;color:#fff;font-weight:900;padding:0 13px;cursor:pointer;white-space:nowrap}.btn.secondary{background:#eaf2f8;color:#06324d;border:1px solid #cde0ef}.btn:hover,.chip:hover{filter:brightness(.98)}.hint{color:var(--muted);font-size:12px;margin-top:8px;line-height:1.35}.chipbar{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.chip{border:1px solid #cfe0ef;background:#fff;border-radius:999px;padding:7px 10px;font-size:12px;color:#164e63;cursor:pointer;font-weight:800}.chip.active{background:#075985;color:#fff;border-color:#075985}.chip.category{background:#f8fafc}.chip.category.active{background:#0f766e;border-color:#0f766e;color:#fff}
    .content{width:100%;min-width:0}.section-title{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:13px;color:#64748b;text-transform:uppercase;letter-spacing:.04em;font-weight:900;margin:2px 0 10px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:13px;width:100%}.card{background:#fff;border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:14px;display:flex;flex-direction:column;gap:10px;min-width:0}.card-top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.issuer{font-weight:950;color:#06243d;font-size:16px;line-height:1.25}.date{white-space:nowrap;color:#64748b;font-size:12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:999px;padding:5px 8px}.badges{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}.badge{background:#eef7ff;color:#075985;border:1px solid #cde5f8;border-radius:999px;padding:4px 8px;font-size:11px;font-weight:800}.badge.type{background:#f8fafc;color:#475569;border-color:#e2e8f0}.title{font-size:15px;font-weight:900;line-height:1.38}.summary{font-size:14px;line-height:1.58;color:#1f2937;white-space:pre-wrap;overflow-wrap:anywhere}.keywords{font-size:12px;line-height:1.45;color:#475569;background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;padding:8px}.source{margin-top:auto;font-size:12px}.kw,.hit{color:var(--red);font-weight:950;background:#ffe4e6;border-radius:4px;padding:0 2px}.hidden{display:none!important}.no-results{display:none;background:#fff;border:1px dashed #cbd5e1;border-radius:18px;padding:26px;text-align:center;color:#64748b}
    .table-wrap{display:none;background:#fff;border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);width:100%;overflow:visible}.table-view{width:100%;border-collapse:collapse;table-layout:fixed}.table-view th{position:sticky;top:72px;background:#06243d;color:#fff;text-align:left;padding:10px;font-size:12px;z-index:3}.table-view td{border-bottom:1px solid #eef2f7;padding:10px;vertical-align:top;font-size:13px;line-height:1.5;overflow-wrap:anywhere}.table-view th:nth-child(1),.table-view td:nth-child(1){width:115px}.table-view th:nth-child(2),.table-view td:nth-child(2){width:170px}.table-view th:nth-child(3),.table-view td:nth-child(3){width:170px}.table-view th:nth-child(4),.table-view td:nth-child(4){width:26%}.view-table .cards{display:none}.view-table .table-wrap{display:block}.compact .summary{display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical;overflow:hidden}.compact .card{gap:8px}
    @media(max-width:1100px){.toolbar-grid{grid-template-columns:1fr 1fr}.buttons{grid-column:1/-1}.hero{flex-direction:column}.stats{justify-content:flex-start}.table-view{table-layout:auto}.table-view th,.table-view td{width:auto!important}}
    @media(max-width:720px){.app{padding:10px}.toolbar{position:static}.toolbar-grid{grid-template-columns:1fr}.cards{grid-template-columns:1fr}.hero h1{font-size:21px}.table-wrap{overflow-x:auto}.table-view{min-width:850px}}
    @media print{.toolbar{display:none}.app{padding:0}.card{break-inside:avoid;box-shadow:none}.table-view th{position:static}}
    """

    js = """
    const ROWS = __DATA__;
    const LT_ENDINGS=['as','is','us','ys','ias','ė','a','o','ui','ų','u','iu','ių','e','ei','ėms','ems','ame','uose','ose','oje','ėje','os','es','omis','ais','iais','ai','ą','į','ę','ės','io','čio','čią','čių','imas','imo','imui','imu','imai','imų',''];
    let activeCategory = '';
    function escReg(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}
    function stem(t){t=(t||'').toLowerCase().trim(); if(t.length<=4) return t; const sorted=[...LT_ENDINGS].sort((a,b)=>b.length-a.length); for(const e of sorted){if(e && t.endsWith(e) && t.length-e.length>=4) return t.slice(0,-e.length)} return t}
    function tokens(q){return (q||'').toLowerCase().split(/\s+/).map(x=>x.trim()).filter(Boolean)}
    function patternsFor(q){return tokens(q).map(t=>new RegExp(escReg(stem(t))+'[a-ząčęėįšųūž]*','iu'))}
    function rowMatches(row,q,issuer,cat){const txt=(row.text||'').toLowerCase(); if(issuer && row.issuer!==issuer) return false; if(cat && !(row.cats||[]).includes(cat)) return false; const ps=patternsFor(q); if(!ps.length) return true; return ps.every(p=>p.test(txt))}
    function clearHighlights(el){if(!el) return; el.querySelectorAll('mark.hit').forEach(m=>{m.replaceWith(document.createTextNode(m.textContent));}); el.normalize()}
    function highlightIn(el,q){if(!el) return; clearHighlights(el); const ps=patternsFor(q); if(!ps.length) return; const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT,{acceptNode:n=> n.parentElement && !['SCRIPT','STYLE'].includes(n.parentElement.tagName) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT}); const nodes=[]; while(walker.nextNode()) nodes.push(walker.currentNode); for(const node of nodes){let text=node.nodeValue; let html=text; for(const p of ps){html=html.replace(p,m=>`<mark class="hit">${m}</mark>`)} if(html!==text){const span=document.createElement('span'); span.innerHTML=html; node.replaceWith(span)}}}
    function applyFilters(){const q=document.getElementById('q').value||''; const issuer=document.getElementById('issuerFilter').value||''; const cat=activeCategory || (document.getElementById('catFilter').value||''); let visible=0; for(const row of ROWS){const keep=rowMatches(row,q,issuer,cat); visible+=keep?1:0; document.querySelectorAll(`[data-row="${row.id}"]`).forEach(el=>{el.classList.toggle('hidden',!keep); highlightIn(el,q);});} document.getElementById('visibleCount').textContent=visible; document.getElementById('noResults').style.display=visible?'none':'block'}
    function resetFilters(){document.getElementById('q').value='';document.getElementById('issuerFilter').value='';document.getElementById('catFilter').value='';activeCategory='';document.querySelectorAll('.chip.category').forEach(b=>b.classList.remove('active'));applyFilters()}
    function setView(v){document.body.classList.toggle('view-table',v==='table');document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===v)); setTimeout(()=>window.dispatchEvent(new Event('resize')),50)}
    function toggleCompact(){document.body.classList.toggle('compact')}
    function setCategory(cat){activeCategory = activeCategory===cat ? '' : cat; document.getElementById('catFilter').value=activeCategory; document.querySelectorAll('.chip.category').forEach(b=>b.classList.toggle('active',b.dataset.cat===activeCategory)); applyFilters()}
    function syncCategorySelect(){activeCategory=document.getElementById('catFilter').value||''; document.querySelectorAll('.chip.category').forEach(b=>b.classList.toggle('active',b.dataset.cat===activeCategory)); applyFilters()}
    document.addEventListener('DOMContentLoaded',()=>{applyFilters();});
    """.replace('__DATA__', data_json)

    parts = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append(f"<title>{html_lib.escape(title)}</title><style>{css}</style></head><body>")
    parts.append("<div class='app'>")
    parts.append("<section class='hero'><div><h1>" + html_lib.escape(title) + "</h1>")
    parts.append(f"<div class='meta'>Generuota: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · Vaizdas naudoja visą lango plotį · nėra vidinio emitentų meniu ir nepatogių slankiklių · raudonai pažymėti raktiniai žodžiai ir paieškos atitikmenys</div></div>")
    parts.append("<div class='stats'>")
    parts.append(f"<div class='stat'><b id='visibleCount'>{len(df_tmp)}</b><span>rodoma</span></div>")
    parts.append(f"<div class='stat'><b>{len(df_tmp)}</b><span>iš viso įrašų</span></div>")
    parts.append(f"<div class='stat'><b>{len(issuers)}</b><span>emitentų</span></div>")
    parts.append("</div></section>")

    parts.append("<section class='toolbar'><div class='toolbar-grid'>")
    parts.append("<div class='field'><label>Paieška pagal žodį ar frazę</label><input id='q' oninput='applyFilters()' placeholder='Pvz. teism, teisminis, dividendai, vadovo, obligacijų...' autocomplete='off'></div>")
    parts.append("<div class='field'><label>Emitentas</label><select id='issuerFilter' onchange='applyFilters()'><option value=''>Visi emitentai</option>")
    for issuer in issuers:
        parts.append(f"<option value='{html_lib.escape(str(issuer), quote=True)}'>{html_lib.escape(str(issuer))}</option>")
    parts.append("</select></div>")
    parts.append("<div class='field'><label>Kategorija</label><select id='catFilter' onchange='syncCategorySelect()'><option value=''>Visos kategorijos</option>")
    for cat in all_categories:
        parts.append(f"<option value='{html_lib.escape(cat, quote=True)}'>{html_lib.escape(cat)}</option>")
    parts.append("</select></div>")
    parts.append("<div class='buttons'><button class='btn secondary' onclick='resetFilters()'>Išvalyti</button><button class='btn secondary' onclick='toggleCompact()'>Kompaktiškai</button></div>")
    parts.append("</div><div class='chipbar'><button class='chip active' data-view='cards' onclick=\"setView('cards')\">Kortelės</button><button class='chip' data-view='table' onclick=\"setView('table')\">Lentelė</button>")
    for cat in all_categories[:12]:
        parts.append(f"<button class='chip category' data-cat='{html_lib.escape(cat, quote=True)}' onclick=\"setCategory('{html_lib.escape(cat, quote=True)}')\">{html_lib.escape(cat)}</button>")
    parts.append("<span class='hint'>Paieška tikrina antraštę, santrauką, emitentą, kategorijas ir raktinius žodžius. Įvedus kamieną, pvz. <b>teism</b>, randamos ir kitos galūnės.</span></div></section>")

    parts.append("<main class='content'>")
    parts.append("<div class='section-title'><span>Kortelių vaizdas</span><span>Pasirinkite lentelės režimą, jeigu norite tankesnio palyginimo.</span></div><section class='cards'>")

    table_rows = []
    for i, r in df_tmp.iterrows():
        rid = f"row_{i}"
        issuer = str(r.get('issuer','Unknown') or 'Unknown')
        date_raw = r.get('date','')
        dt = pd.to_datetime(date_raw, errors='coerce')
        date_fmt = dt.strftime('%Y-%m-%d %H:%M') if pd.notna(dt) else str(date_raw)
        title_raw = str(r.get('title','') or '')
        summary_raw = str(r.get('summary','') or '')
        url = str(r.get('url','') or '').strip()
        typ = str(r.get('type','') or '').strip()
        cats = row_categories(r)
        matched = str(r.get('matched_keywords','') or '').strip()
        title_html = highlight_keywords(title_raw)
        summary_html = highlight_keywords(summary_raw)
        matched_html = highlight_keywords(matched)
        badges = ''.join(f"<span class='badge'>{html_lib.escape(c)}</span>" for c in cats)
        if typ:
            badges += f"<span class='badge type'>{html_lib.escape(typ)}</span>"
        title_block = f"<a href='{html_lib.escape(url, quote=True)}' target='_blank' rel='noreferrer'>{title_html}</a>" if url else title_html
        source = f"<div class='source'><a href='{html_lib.escape(url, quote=True)}' target='_blank' rel='noreferrer'>Atidaryti šaltinį ↗</a></div>" if url else ""
        keywords = f"<div class='keywords'><b>Raktiniai žodžiai:</b> {matched_html}</div>" if matched else ""
        parts.append(f"""
        <article class='card' data-row='{rid}'>
          <div class='card-top'><div><div class='issuer'>{html_lib.escape(issuer)}</div><div class='badges'>{badges}</div></div><div class='date'>{html_lib.escape(date_fmt)}</div></div>
          <div class='title'>{title_block}</div>
          <div class='summary'>{summary_html}</div>
          {keywords}{source}
        </article>
        """)
        table_rows.append(f"""
        <tr data-row='{rid}'>
          <td>{html_lib.escape(date_fmt)}</td><td>{html_lib.escape(issuer)}</td><td>{badges}</td>
          <td><b>{title_block}</b></td><td>{summary_html}{keywords}</td>
        </tr>
        """)
    parts.append("</section><div id='noResults' class='no-results'>Pagal pasirinktą paiešką ir filtrus įrašų nerasta.</div>")
    parts.append("<section class='table-wrap'><table class='table-view'><thead><tr><th>Data</th><th>Emitentas</th><th>Kategorijos</th><th>Antraštė</th><th>Santrauka / raktiniai žodžiai</th></tr></thead><tbody>")
    parts.extend(table_rows)
    parts.append("</tbody></table></section>")
    parts.append("</main></div>")
    parts.append(f"<script>{js}</script></body></html>")
    return "\n".join(parts)


# ============================================================
# STREAMLIT LENTELĖS PARUOŠIMAS
# ============================================================

def prepare_streamlit_view_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=[
            "data",
            "emitentas",
            "kategorijos",
            "tipas",
            "antraste",
            "santrauka",
            "raktazodziai",
            "nuoroda",
        ])

    out = df.copy()

    out["date_parsed"] = parse_dates_safe(out["date"])
    out["data"] = out["date_parsed"].dt.strftime("%Y-%m-%d %H:%M")
    out["data"] = out["data"].fillna(out["date"].astype(str))

    out["kategorijos"] = out["categories"].apply(
        lambda x: ", ".join(x) if isinstance(x, list) else str(x)
    )

    out["emitentas"] = out["issuer"].fillna("Unknown").astype(str)
    out["tipas"] = out["type"].fillna("").astype(str)
    out["antraste"] = out["title"].fillna("").astype(str)
    out["santrauka"] = out["summary"].fillna("").astype(str)
    out["raktazodziai"] = out["matched_keywords"].fillna("").astype(str)
    out["nuoroda"] = out["url"].fillna("").astype(str)

    out = out.sort_values(
        by=["date_parsed", "data"],
        ascending=[False, False],
    )

    return out[[
        "data",
        "emitentas",
        "kategorijos",
        "tipas",
        "antraste",
        "santrauka",
        "raktazodziai",
        "nuoroda",
    ]]


def filter_streamlit_df(
    df_view: pd.DataFrame,
    search: str = "",
    selected_issuers: Optional[list] = None,
    selected_categories: Optional[list] = None,
) -> pd.DataFrame:
    if df_view is None or df_view.empty:
        return df_view

    out = df_view.copy()

    if selected_issuers:
        out = out[out["emitentas"].isin(selected_issuers)]

    if selected_categories:
        mask_cat = out["kategorijos"].astype(str).apply(
            lambda val: any(cat in val for cat in selected_categories)
        )
        out = out[mask_cat]

    if search and search.strip():
        q = search.strip().lower()

        searchable_cols = [
            "data",
            "emitentas",
            "kategorijos",
            "tipas",
            "antraste",
            "santrauka",
            "raktazodziai",
        ]

        mask = pd.Series(False, index=out.index)

        for col in searchable_cols:
            mask = mask | out[col].astype(str).str.lower().str.contains(
                q,
                na=False,
                regex=False,
            )

        out = out[mask]

    return out


# ============================================================
# VIEŠOS FUNKCIJOS STREAMLIT INTEGRACIJAI
# ============================================================

def generate_emitentu_ataskaita(
    start_date: date,
    end_date: date,
    title: Optional[str] = None,
) -> dict:
    if start_date is None or end_date is None:
        raise ValueError("Reikia nurodyti start_date ir end_date.")

    if start_date > end_date:
        raise ValueError("Data 'Nuo' negali būti vėlesnė už datą 'Iki'.")

    raw_df = load_crib_news_for_report(start_date, end_date)
    df = prepare_classified_df(raw_df)

    if title is None:
        title = f"Emitentų atranka pagal CRIB naujienas ({start_date} – {end_date})"

    html = build_pretty_html(df, title=title)

    return {
        "html": html,
        "df": df,
        "start_date": start_date,
        "end_date": end_date,
    }


def save_emitentu_ataskaita_html(
    start_date: date,
    end_date: date,
    output_path: str | Path,
) -> Path:
    result = generate_emitentu_ataskaita(start_date, end_date)

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result["html"], encoding="utf-8")

    return path


def render_emitentu_atranka_page(default_days: int = 30):
    import streamlit as st
    import streamlit.components.v1 as components

    st.subheader("🧾 Emitentų atranka pagal CRIB naujienas")
    st.caption("Duomenys imami iš Supabase market_news lentelės, source='crib'.")

    today = date.today()

    col1, col2 = st.columns(2)

    with col1:
        start = st.date_input(
            "Nuo",
            today - timedelta(days=default_days),
            key="crib_cls_start",
        )

    with col2:
        end = st.date_input(
            "Iki",
            today,
            key="crib_cls_end",
        )

    if start > end:
        st.error("Data 'Nuo' negali būti vėlesnė už datą 'Iki'.")
        return

    generate_clicked = st.button(
        "Generuoti klasifikavimo ataskaitą",
        key="crib_cls_generate",
    )

    if generate_clicked:
        st.session_state.pop("crib_cls_result", None)

        with st.spinner("Kraunamos CRIB naujienos iš duomenų bazės ir generuojama ataskaita..."):
            result = generate_emitentu_ataskaita(start, end)

        st.session_state["crib_cls_result"] = result
        st.session_state["crib_cls_start_used"] = start
        st.session_state["crib_cls_end_used"] = end

    result = st.session_state.get("crib_cls_result")

    if not result:
        st.info("Pasirinkite laikotarpį ir spauskite „Generuoti klasifikavimo ataskaitą“.")
        return

    st.success(f"Rasta įrašų: {len(result['df'])}")

    tab_table, tab_html, tab_export = st.tabs([
        "📋 Interaktyvi lentelė",
        "🌐 HTML peržiūra",
        "⬇️ Atsisiuntimas",
    ])

    with tab_table:
        df_view = prepare_streamlit_view_df(result["df"])

        if df_view.empty:
            st.info("Pasirinktu laikotarpiu įrašų nerasta.")
        else:
            st.markdown("#### Paieška ir filtrai")

            search = st.text_input(
                "Paieškos žodis",
                placeholder="Pvz. teism, dividendai, vadovas, nuostoliai, obligacijos...",
                key="crib_cls_search",
            )

            filter_col1, filter_col2 = st.columns(2)

            with filter_col1:
                issuers = sorted(df_view["emitentas"].dropna().unique().tolist())

                selected_issuers = st.multiselect(
                    "Emitentai",
                    options=issuers,
                    default=[],
                    key="crib_cls_issuers_filter",
                )

            with filter_col2:
                all_categories = sorted({
                    cat.strip()
                    for value in df_view["kategorijos"].dropna().astype(str)
                    for cat in value.split(",")
                    if cat.strip()
                })

                selected_categories = st.multiselect(
                    "Kategorijos",
                    options=all_categories,
                    default=[],
                    key="crib_cls_categories_filter",
                )

            filtered = filter_streamlit_df(
                df_view=df_view,
                search=search,
                selected_issuers=selected_issuers,
                selected_categories=selected_categories,
            )

            st.caption(f"Rodoma įrašų: {len(filtered)} iš {len(df_view)}")

            st.dataframe(
                filtered,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "data": st.column_config.TextColumn("Data", width="small"),
                    "emitentas": st.column_config.TextColumn("Emitentas", width="medium"),
                    "kategorijos": st.column_config.TextColumn("Kategorijos", width="medium"),
                    "tipas": st.column_config.TextColumn("Tipas", width="medium"),
                    "antraste": st.column_config.TextColumn("Antraštė", width="large"),
                    "santrauka": st.column_config.TextColumn("Santrauka", width="large"),
                    "raktazodziai": st.column_config.TextColumn("Raktažodžiai", width="medium"),
                    "nuoroda": st.column_config.LinkColumn("Nuoroda"),
                },
            )

    with tab_html:
        components.html(
            result["html"],
            height=900,
            scrolling=True,
        )

    with tab_export:
        st.markdown("#### Atsisiuntimai")

        st.download_button(
            "Atsisiųsti HTML ataskaitą",
            data=result["html"].encode("utf-8"),
            file_name=f"crib_klasifikacija_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.html",
            mime="text/html",
        )

        df_export = prepare_streamlit_view_df(result["df"])
        csv_data = df_export.to_csv(index=False).encode("utf-8-sig")

        st.download_button(
            "Atsisiųsti lentelę CSV formatu",
            data=csv_data,
            file_name=f"crib_klasifikacija_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    res = generate_emitentu_ataskaita(
        date.today() - timedelta(days=30),
        date.today(),
    )

    out = Path(f"crib_klasifikacija_{date.today().strftime('%Y%m%d')}.html")
    out.write_text(res["html"], encoding="utf-8")

    print(f"Išsaugota: {out.resolve()}")
    print(f"Įrašų skaičius: {len(res['df'])}")

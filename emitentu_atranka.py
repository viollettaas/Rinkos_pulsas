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
    """Moderni HTML ataskaita su paieska, filtrais, kortelemis ir lentele.

    Viskas veikia viename HTML faile: nereikia Streamlit interaktyvios lenteles.
    Paieska atlieka paprasta lietuvisku galuniu tolerancija ir paryskina rastus zodzius.
    """
    if df is None or df.empty:
        return f"""
        <!doctype html>
        <html>
        <head><meta charset='utf-8'></head>
        <body style='font-family:Inter,Segoe UI,Arial,sans-serif;padding:24px;background:#f6f8fb;color:#111827'>
            <h2>{html_lib.escape(title)}</h2>
            <p>Nurodytame laikotarpyje CRIB naujienų duomenų bazėje nerasta.</p>
        </body>
        </html>
        """

    df_tmp = df.copy()
    df_tmp["date_parsed"] = parse_dates_safe(df_tmp["date"])
    df_tmp = df_tmp.sort_values(["date_parsed", "orig_order"], ascending=[False, True]).reset_index(drop=True)

    def row_categories(row):
        vals = []
        src = str(row.get("category_src", "") or "").strip()
        if src:
            vals.append(src)
        cats = row.get("categories") or []
        if isinstance(cats, str):
            cats = [cats]
        for c in cats:
            c = str(c or "").strip()
            if c and c not in vals:
                vals.append(c)
        return vals or ["Kiti"]

    all_categories = sorted({c for _, r in df_tmp.iterrows() for c in row_categories(r)}) or CATEGORY_ORDER.copy()
    issuers = df_tmp["issuer"].fillna("Unknown").astype(str).tolist()
    issuer_order = df_tmp["issuer"].fillna("Unknown").astype(str).drop_duplicates().tolist()

    # JSON duomenys JS filtrams. Tekstai HTML'e jau saugiai escapinami atskirai.
    import json
    rows_json = []
    for i, r in df_tmp.iterrows():
        cats = row_categories(r)
        combined = " ".join([
            str(r.get("issuer", "")), str(r.get("title", "")), str(r.get("summary", "")),
            str(r.get("type", "")), str(r.get("matched_keywords", "")), " ".join(cats)
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
      --shadow:0 14px 35px rgba(15,23,42,.08);--radius:18px;
    }
    *{box-sizing:border-box}
    html,body{margin:0;min-height:100%;font-family:Inter,Segoe UI,Arial,Helvetica,sans-serif;background:var(--bg);color:var(--text)}
    a{color:#075985;text-decoration:none} a:hover{text-decoration:underline}
    .app{width:100%;max-width:none;margin:0;padding:18px}
    .hero{background:linear-gradient(135deg,#ffffff 0%,#eff8ff 56%,#dceeff 100%);border:1px solid #d7e8f7;border-radius:22px;padding:18px 20px;box-shadow:var(--shadow);display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:14px}
    .hero h1{margin:0;color:#06243d;font-size:26px;line-height:1.15}.hero .meta{margin-top:8px;color:var(--muted);font-size:13px}.stats{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}.stat{background:#fff;border:1px solid var(--line);border-radius:15px;padding:10px 14px;min-width:110px}.stat b{font-size:22px;color:#06243d}.stat span{display:block;color:var(--muted);font-size:12px;margin-top:2px}
    .toolbar{position:sticky;top:0;z-index:20;background:rgba(245,247,251,.94);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:18px;box-shadow:0 10px 28px rgba(15,23,42,.07);padding:12px;margin-bottom:14px}
    .toolbar-grid{display:grid;grid-template-columns:minmax(260px,1.4fr) minmax(200px,.7fr) minmax(190px,.7fr) auto;gap:10px;align-items:end}.field label{display:block;font-size:12px;color:#475569;font-weight:800;margin:0 0 5px 2px}.field input,.field select{width:100%;height:42px;border:1px solid #cfddeb;background:#fff;border-radius:12px;padding:0 12px;color:#0f172a;font-size:14px}.buttons{display:flex;gap:8px;flex-wrap:wrap}.btn{height:42px;border:0;border-radius:12px;background:#075985;color:#fff;font-weight:900;padding:0 13px;cursor:pointer}.btn.secondary{background:#eaf2f8;color:#06324d;border:1px solid #cde0ef}.btn.warn{background:#fff7ed;color:#9a3412;border:1px solid #fed7aa}.hint{color:var(--muted);font-size:12px;margin-top:8px;line-height:1.35}.chipbar{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.chip{border:1px solid #cfe0ef;background:#fff;border-radius:999px;padding:6px 9px;font-size:12px;color:#164e63;cursor:pointer}.chip.active{background:#075985;color:#fff;border-color:#075985}
    .layout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:14px}.side{position:sticky;top:118px;align-self:start;background:#fff;border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:13px;max-height:calc(100vh - 135px);overflow:auto}.side h3{margin:0 0 10px;color:#0f2f45;font-size:15px}.issuer-link{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 9px;border-radius:12px;color:#0f3f5b;font-size:13px}.issuer-link:hover{background:#eef7ff;text-decoration:none}.count{background:#e2edf7;color:#075985;border-radius:999px;padding:2px 7px;font-size:11px;font-weight:900}.content{min-width:0}.section-title{font-size:13px;color:#64748b;text-transform:uppercase;letter-spacing:.04em;font-weight:900;margin:4px 0 10px}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:13px}.card{background:#fff;border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:14px;display:flex;flex-direction:column;gap:10px}.card-top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.issuer{font-weight:950;color:#06243d;font-size:16px}.date{white-space:nowrap;color:#64748b;font-size:12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:999px;padding:5px 8px}.badges{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}.badge{background:#eef7ff;color:#075985;border:1px solid #cde5f8;border-radius:999px;padding:4px 8px;font-size:11px;font-weight:800}.badge.type{background:#f8fafc;color:#475569;border-color:#e2e8f0}.title{font-size:15px;font-weight:900;line-height:1.35}.summary{font-size:14px;line-height:1.55;color:#1f2937;white-space:pre-wrap}.keywords{font-size:12px;line-height:1.45;color:#475569;background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;padding:8px}.source{margin-top:auto;font-size:12px}.kw,.hit{color:var(--red);font-weight:950;background:#ffe4e6;border-radius:4px;padding:0 2px}.table-wrap{display:none;background:#fff;border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);overflow:auto}.table-view{width:100%;border-collapse:collapse;table-layout:auto}.table-view th{position:sticky;top:0;background:#06243d;color:#fff;text-align:left;padding:10px;font-size:12px;z-index:3}.table-view td{border-bottom:1px solid #eef2f7;padding:10px;vertical-align:top;font-size:13px;line-height:1.45}.table-view .summary-cell{min-width:420px}.hidden{display:none!important}.no-results{display:none;background:#fff;border:1px dashed #cbd5e1;border-radius:18px;padding:26px;text-align:center;color:#64748b}.compact .summary{max-height:110px;overflow:auto}.compact .card{gap:8px}.view-table .cards{display:none}.view-table .table-wrap{display:block}
    @media(max-width:1100px){.toolbar-grid{grid-template-columns:1fr 1fr}.layout{grid-template-columns:1fr}.side{position:static;max-height:260px}.cards{grid-template-columns:1fr}}
    @media(max-width:650px){.app{padding:10px}.hero{flex-direction:column}.toolbar-grid{grid-template-columns:1fr}.cards{grid-template-columns:1fr}.date{white-space:normal}.card-top{flex-direction:column}.side{display:none}}
    @media print{.toolbar,.side{display:none}.layout{display:block}.cards{grid-template-columns:1fr}.card{break-inside:avoid;box-shadow:none}.app{padding:0}.hero{box-shadow:none}}
    """

    js = r"""
    const ROWS = __DATA__;
    const LT_ENDINGS = ['iaus','iui','ių','io','iu','į','is','ys','us','as','ais','uose','ose','oje','ėje','omis','ams','iems','es','ės','ė','ą','ų','ui','uo','a','o','e','ai','ei','am','em',''];
    function escReg(s){return (s||'').replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}
    function tokens(q){return (q||'').toLowerCase().match(/[\wąčęėįšųūž]+/giu)||[]}
    function stem(w){w=(w||'').toLowerCase(); if(w.length<=4) return w; const sorted=[...LT_ENDINGS].sort((a,b)=>b.length-a.length); for(const e of sorted){ if(e && w.endsWith(e) && w.length-e.length>=4) return w.slice(0,-e.length)} return w}
    function patternsFor(q){const out=[]; for(const t of tokens(q)){const vars=[...new Set([t,stem(t)])]; for(const v of vars){if(v.length<3) continue; const ends=LT_ENDINGS.filter(Boolean).sort((a,b)=>b.length-a.length).map(escReg).join('|'); out.push(new RegExp('\\b'+escReg(v)+'(?:'+ends+')?\\b','giu'))}} return out}
    function rowMatches(row, q, issuer, cat){
      const txt=(row.text||'').toLowerCase();
      if(issuer && row.issuer!==issuer) return false;
      if(cat && !(row.cats||[]).includes(cat)) return false;
      const ts=tokens(q); if(!ts.length) return true;
      return ts.every(t=>patternsFor(t).some(p=>p.test(txt)));
    }
    function clearHighlights(el){
      if(!el) return; el.querySelectorAll('mark.hit').forEach(m=>{m.replaceWith(document.createTextNode(m.textContent));}); el.normalize();
    }
    function highlightIn(el, q){
      if(!el) return; clearHighlights(el); const pats=patternsFor(q); if(!pats.length) return;
      const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT,{acceptNode:n=> n.parentElement && !['SCRIPT','STYLE','A'].includes(n.parentElement.tagName) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT});
      const nodes=[]; while(walker.nextNode()) nodes.push(walker.currentNode);
      for(const node of nodes){let text=node.nodeValue; let html=text; for(const p of pats){html=html.replace(p,m=>`<mark class="hit">${m}</mark>`)} if(html!==text){const span=document.createElement('span'); span.innerHTML=html; node.replaceWith(span)}}
    }
    function applyFilters(){
      const q=document.getElementById('q').value||'';
      const issuer=document.getElementById('issuerFilter').value||'';
      const cat=document.getElementById('catFilter').value||'';
      let visible=0;
      for(const row of ROWS){
        const keep=rowMatches(row,q,issuer,cat); visible+=keep?1:0;
        document.querySelectorAll(`[data-row="${row.id}"]`).forEach(el=>{el.classList.toggle('hidden',!keep); highlightIn(el,q);});
      }
      document.getElementById('visibleCount').textContent=visible;
      document.getElementById('noResults').style.display=visible?'none':'block';
      document.querySelectorAll('.issuer-link').forEach(a=>{const iss=a.dataset.issuer; const cnt=ROWS.filter(r=>r.issuer===iss && rowMatches(r,q,'',cat)).length; a.querySelector('.count').textContent=cnt; a.classList.toggle('hidden',cnt===0)});
    }
    function resetFilters(){document.getElementById('q').value='';document.getElementById('issuerFilter').value='';document.getElementById('catFilter').value='';applyFilters()}
    function setView(v){document.body.classList.toggle('view-table',v==='table');document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===v))}
    function toggleCompact(){document.body.classList.toggle('compact')}
    function goIssuer(issuer){document.getElementById('issuerFilter').value=issuer; applyFilters(); window.scrollTo({top:0,behavior:'smooth'});}
    document.addEventListener('DOMContentLoaded',()=>{applyFilters();});
    """.replace('__DATA__', data_json)

    parts = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append(f"<title>{html_lib.escape(title)}</title><style>{css}</style></head><body>")
    parts.append("<div class='app'>")
    parts.append("<section class='hero'><div><h1>" + html_lib.escape(title) + "</h1>")
    parts.append(f"<div class='meta'>Generuota: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · Visi tekstai rodomi pilnai · raudonai pažymėti raktiniai žodžiai ir paieškos atitikmenys</div></div>")
    parts.append("<div class='stats'>")
    parts.append(f"<div class='stat'><b id='visibleCount'>{len(df_tmp)}</b><span>rodoma</span></div>")
    parts.append(f"<div class='stat'><b>{len(df_tmp)}</b><span>iš viso įrašų</span></div>")
    parts.append(f"<div class='stat'><b>{len(set(issuers))}</b><span>emitentų</span></div>")
    parts.append("</div></section>")

    parts.append("<section class='toolbar'><div class='toolbar-grid'>")
    parts.append("<div class='field'><label>Paieška pagal žodį ar frazę</label><input id='q' oninput='applyFilters()' placeholder='Pvz. teism, teisminis, dividendai, vadovo, obligacijų...' autocomplete='off'></div>")
    parts.append("<div class='field'><label>Emitentas</label><select id='issuerFilter' onchange='applyFilters()'><option value=''>Visi emitentai</option>")
    for issuer in issuer_order:
        parts.append(f"<option value='{html_lib.escape(str(issuer), quote=True)}'>{html_lib.escape(str(issuer))}</option>")
    parts.append("</select></div>")
    parts.append("<div class='field'><label>Kategorija</label><select id='catFilter' onchange='applyFilters()'><option value=''>Visos kategorijos</option>")
    for cat in all_categories:
        parts.append(f"<option value='{html_lib.escape(cat, quote=True)}'>{html_lib.escape(cat)}</option>")
    parts.append("</select></div>")
    parts.append("<div class='buttons'><button class='btn secondary' onclick='resetFilters()'>Išvalyti</button><button class='btn secondary' onclick='toggleCompact()'>Kompaktiškai</button></div>")
    parts.append("</div><div class='chipbar'><button class='chip active' data-view='cards' onclick=\"setView('cards')\">Kortelės</button><button class='chip' data-view='table' onclick=\"setView('table')\">Lentelė</button><span class='hint'>Paieška tikrina antraštę, santrauką, emitentą, kategorijas ir raktinius žodžius. Pvz., įvedus <b>teism</b>, ras ir „teismo“, „teismui“, „teisminis“ tipo atitikmenis pagal kamieną.</span></div></section>")

    parts.append("<div class='layout'><aside class='side'><h3>Emitentai</h3>")
    for issuer in issuer_order:
        cnt = int((df_tmp['issuer'].fillna('Unknown').astype(str) == str(issuer)).sum())
        parts.append(f"<a class='issuer-link' data-issuer='{html_lib.escape(str(issuer), quote=True)}' href='javascript:void(0)' onclick=\"goIssuer('{html_lib.escape(str(issuer), quote=True)}')\"><span>{html_lib.escape(str(issuer))}</span><span class='count'>{cnt}</span></a>")
    parts.append("</aside><main class='content'>")
    parts.append("<div class='section-title'>Kortelių vaizdas</div><section class='cards'>")

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
          <td><b>{title_block}</b></td><td class='summary-cell'>{summary_html}{keywords}</td>
        </tr>
        """)
    parts.append("</section><div id='noResults' class='no-results'>Pagal pasirinktą paiešką ir filtrus įrašų nerasta.</div>")
    parts.append("<section class='table-wrap'><table class='table-view'><thead><tr><th>Data</th><th>Emitentas</th><th>Kategorijos</th><th>Antraštė</th><th>Santrauka / raktiniai žodžiai</th></tr></thead><tbody>")
    parts.extend(table_rows)
    parts.append("</tbody></table></section>")
    parts.append("</main></div></div>")
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

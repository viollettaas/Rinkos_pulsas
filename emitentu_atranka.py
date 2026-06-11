
import re
import html as html_lib
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Iterable, Optional

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
    """Sukuria paprasta lietuvisku galuniu regex sablona raktazodziui."""
    if any(ch in stem for ch in r".^$*+?{}[]\|()"):
        return stem
    esc_stem = re.escape(stem)
    nonempty = [re.escape(e) for e in sorted(set(_LIT_ENDINGS)) if e]
    group = "|".join(nonempty) if nonempty else ""
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
# PAGALBINES FUNKCIJOS
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
                escaped = pat.sub(lambda m: f"<strong class='kw'>{m.group(0)}</strong>", escaped)
            except re.error:
                continue
    return escaped


# ============================================================
# SUPABASE -> ATASKAITOS DATAFRAME
# ============================================================


def load_crib_news_for_report(start_date: date, end_date: date) -> pd.DataFrame:
    """
    Ima CRIB naujienas is Supabase ir konvertuoja i ataskaitai tinkama forma.

    Tavo market_news lenteleje tiketini stulpeliai:
    - source
    - company
    - company_norm
    - category
    - title
    - url
    - published_at
    - content
    """
    raw = load_news_df("crib", start_date, end_date)

    if raw is None or raw.empty:
        return pd.DataFrame(columns=[
            "date", "issuer", "type", "category_src", "title", "url", "summary", "orig_order",
        ])

    df = raw.copy()

    # Saugus stulpeliu paemimas, kad nesugriutu, jei kazkurio truksta.
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
            "orig_order", "combined_text", "categories", "categories_str",
        ])

    df = df.copy().reset_index(drop=True)
    df["title"] = df["title"].fillna("").astype(str)
    df["summary"] = df["summary"].fillna("").astype(str)
    df["type"] = df.get("type", "").fillna("").astype(str)
    df["issuer"] = df["issuer"].fillna("Unknown").astype(str)
    df["category_src"] = df.get("category_src", "").fillna("").astype(str)

    if "orig_order" not in df.columns:
        df["orig_order"] = df.index

    df["combined_text"] = (df["title"] + " " + df["summary"] + " " + df["type"]).str.lower()

    assigned = []
    for _, row in df.iterrows():
        text = (row["title"] + " " + row["summary"] + " " + row["type"]).lower()
        cats = classify_row_text(text, allow_multiple=True)
        assigned.append(cats)

    df["categories"] = assigned
    df["categories_str"] = df["categories"].apply(
        lambda x: ";".join(x) if isinstance(x, (list, tuple)) else str(x)
    )
    return df


# ============================================================
# HTML GENERAVIMAS
# ============================================================


def build_pretty_html(df: pd.DataFrame, title: str = "Emitentų atranka pagal CRIB naujienas") -> str:
    if df is None or df.empty:
        return f"""
        <!doctype html>
        <html><head><meta charset='utf-8'></head>
        <body style='font-family:Arial,sans-serif;padding:24px'>
            <h2>{html_lib.escape(title)}</h2>
            <p>Nurodytame laikotarpyje CRIB naujienų duomenų bazėje nerasta.</p>
        </body></html>
        """

    expl = defaultdict(int)
    for cats in df.get("categories", []):
        if not cats:
            expl["Kiti"] += 1
        else:
            for c in cats:
                expl[c] += 1
    summary_df = pd.DataFrame(sorted(expl.items(), key=lambda x: -x[1]), columns=["category", "count"])

    cats_from_src = sorted({c.strip() for c in df["category_src"].unique() if c and str(c).strip()})
    cats_assigned = set()
    for row in df.get("categories", []):
        if isinstance(row, (list, tuple)):
            for c in row:
                cats_assigned.add(str(c))
        elif row:
            cats_assigned.add(str(row))
    cats_assigned = sorted(cats_assigned)
    all_categories = sorted(set(cats_from_src + cats_assigned))
    if not all_categories:
        all_categories = CATEGORY_ORDER.copy()

    # Naujausi emitentai pagal pirma naujiena virsuje.
    df_tmp = df.copy()
    df_tmp["date_parsed"] = parse_dates_safe(df_tmp["date"])
    issuer_order = (
        df_tmp.sort_values("date_parsed", ascending=False)["issuer"]
        .drop_duplicates()
        .tolist()
    )

    css = """
    :root{
      --bg:#f6f8fb; --card:#ffffff; --accent:#0b6ea8; --muted:#6b7680;
      --danger:#b30000; --radius:12px; --glass: rgba(11,110,168,0.06);
    }
    html,body{height:100%;margin:0;font-family:Inter,Segoe UI,Arial,Helvetica,sans-serif;background:var(--bg);color:#111}
    .container{max-width:1250px;margin:20px auto;padding:18px}
    header{display:flex;align-items:center;justify-content:space-between;padding:12px 0}
    header h1{margin:0;font-size:1.55rem;color:var(--accent)}
    .meta{color:var(--muted);font-size:0.92rem}
    .top{display:flex;gap:18px;align-items:flex-start}
    .toc{width:330px;background:var(--card);padding:14px;border-radius:12px;box-shadow:0 6px 18px rgba(12,40,60,0.06);position:sticky;top:10px;max-height:92vh;overflow:auto}
    .toc h3{margin:0 0 8px 0}
    .toc input[type="text"]{width:100%;box-sizing:border-box;padding:8px;border-radius:8px;border:1px solid #e7eef6}
    .toc ul{list-style:none;padding:8px 0;margin:10px 0;max-height:360px;overflow:auto}
    .toc li{margin:6px 0}
    .toc a{display:flex;justify-content:space-between;text-decoration:none;color:#0b4860;padding:6px 8px;border-radius:8px}
    .toc a:hover{background:var(--glass)}
    .content{flex:1;margin-left:8px;min-width:0}
    .summary-card{background:linear-gradient(180deg,#fff,#fbfdff);padding:12px;border-radius:12px;box-shadow:0 6px 20px rgba(12,40,60,0.04);margin-bottom:12px}
    .issuer-card{background:var(--card);padding:14px;margin-bottom:14px;border-radius:12px;box-shadow:0 6px 18px rgba(12,40,60,0.04)}
    .issuer-header{display:flex;align-items:center;justify-content:space-between;gap:12px}
    .issuer-title{font-size:1.16rem;font-weight:800;color:#083b50}
    .badge{background:var(--accent);color:white;padding:5px 9px;border-radius:999px;font-weight:600;font-size:0.82rem}
    .cat-title{font-size:1rem;margin:10px 0 6px 0;color:#0b5575}
    table{width:100%;border-collapse:collapse;margin-bottom:8px;table-layout:fixed}
    th,td{padding:8px;border-bottom:1px solid #eef6fb;text-align:left;vertical-align:top;font-size:0.92rem;word-wrap:break-word}
    th{background:#fbfdff;font-weight:700;color:#234}
    th.date-col, td.date-col{width:125px}
    th.title-col, td.title-col{width:40%}
    .muted{color:var(--muted);font-size:0.88rem}
    .controls{display:flex;gap:8px;align-items:center}
    .btn{background:var(--accent);color:white;padding:8px 10px;border-radius:8px;text-decoration:none;font-weight:600;cursor:pointer;border:none}
    .small{font-size:0.84rem;color:var(--muted)}
    .toggle-btn{background:#f3f7fb;border-radius:8px;padding:6px 8px;border:1px solid #e7eef6;cursor:pointer}
    .no-rows{color:var(--muted);padding:8px 0}
    .collapsible{overflow:hidden;transition:max-height .25s ease-out}
    strong.kw{font-weight:800;color:var(--danger)}
    .filter-controls{margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .cat-checkbox{display:flex;align-items:center;gap:8px;padding:4px 0}
    @media(max-width:900px){.top{flex-direction:column}.toc{width:auto;position:static}.content{margin-left:0;width:100%}}
    @media print{.toc,.controls{display:none}.container{max-width:100%}.collapsible{max-height:none!important}}
    """

    js = """
    function scrollToId(id){document.querySelector('#'+id).scrollIntoView({behavior:'smooth',block:'start'});}
    function expandAll(){
      document.querySelectorAll('.collapsible').forEach(div=>{ div.style.maxHeight = div.scrollHeight + 'px'; });
      document.querySelectorAll('[data-toggle]').forEach(btn => btn.innerText = 'Slėpti');
      let g = document.getElementById('global_toggle'); if(g) g.innerText = 'Slėpti visus';
    }
    function collapseAll(){
      document.querySelectorAll('.collapsible').forEach(div=>{ div.style.maxHeight = '0px'; });
      document.querySelectorAll('[data-toggle]').forEach(btn => btn.innerText = 'Rodyti');
      let g = document.getElementById('global_toggle'); if(g) g.innerText = 'Rodyti visus';
    }
    function toggleAll(){
      let anyClosed = false;
      document.querySelectorAll('.collapsible').forEach(div=>{
        if(!div.style.maxHeight || div.style.maxHeight === '0px') anyClosed = true;
      });
      if(anyClosed) expandAll(); else collapseAll();
    }
    document.addEventListener('click', function(e){
      if(e.target.matches('[data-toggle]') || e.target.closest('[data-toggle]')){
        let btn = e.target.closest('[data-toggle]');
        let target = document.querySelector(btn.dataset.toggle);
        if(!target) return;
        if(target.style.maxHeight && target.style.maxHeight !== '0px'){
          target.style.maxHeight = '0px'; btn.innerText = 'Rodyti';
        } else {
          target.style.maxHeight = target.scrollHeight + 'px'; btn.innerText = 'Slėpti';
        }
      }
    });
    function tocFilter(){
      let q = document.getElementById('toc_search').value.trim().toLowerCase();
      document.querySelectorAll('.toc li').forEach(li=>{
        let txt = li.dataset.issuer || '';
        li.style.display = txt.indexOf(q) !== -1 ? '' : 'none';
      });
    }
    function getSelectedCategories(){
      return Array.from(document.querySelectorAll('.cat-filter input[type="checkbox"]:checked')).map(n=>n.value);
    }
    function filterByCategories(){
      const selected = getSelectedCategories();
      const rows = document.querySelectorAll('tr[data-cats]');
      rows.forEach(row=>{
        const cats = (row.dataset.cats || '').toLowerCase().split(';').map(x=>x.trim()).filter(Boolean);
        const keep = cats.some(c => selected.includes(c));
        row.style.display = keep ? '' : 'none';
      });
      document.querySelectorAll('.issuer-card').forEach(card=>{
        const trs = Array.from(card.querySelectorAll('tr[data-cats]'));
        const anyVisible = trs.length ? trs.some(tr => tr.style.display !== 'none') : false;
        card.style.display = anyVisible ? '' : 'none';
      });
    }
    function toggleSelectAllCats(){
      const inputs = Array.from(document.querySelectorAll('.cat-filter input[type="checkbox"]'));
      const anyUnchecked = inputs.some(i=>!i.checked);
      inputs.forEach(i=> i.checked = anyUnchecked );
      filterByCategories();
      const btn = document.getElementById('cat_select_all_btn');
      if(btn) btn.innerText = anyUnchecked ? 'Atžymėti viską' : 'Pažymėti viską';
    }
    document.addEventListener('DOMContentLoaded', function(){
      document.querySelectorAll('.collapsible').forEach(div => div.style.maxHeight = '0px');
      let g = document.getElementById('global_toggle'); if(g) g.innerText = 'Rodyti visus';
      document.querySelectorAll('.cat-filter input[type="checkbox"]').forEach(cb=>{
        cb.addEventListener('change', filterByCategories);
      });
      filterByCategories();
    });
    """

    parts = []
    parts.append("<!doctype html>")
    parts.append("<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append(f"<title>{html_lib.escape(title)}</title><style>{css}</style></head><body>")
    parts.append("<div class='container'>")
    parts.append("<header>")
    parts.append("<div style='display:flex;flex-direction:column'>")
    parts.append(f"<h1>{html_lib.escape(title)}</h1>")
    parts.append(f"<div class='meta'>Generuota: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>")
    parts.append("</div>")
    parts.append("<div class='controls'><button id='global_toggle' class='btn' onclick='toggleAll()'>Rodyti visus</button></div>")
    parts.append("</header>")

    parts.append("<div class='top'>")
    parts.append("<aside class='toc'>")
    parts.append("<h3>Emitentų sąrašas</h3>")
    parts.append("<input id='toc_search' placeholder='Ieškoti emitento...' oninput='tocFilter()' />")

    parts.append("<div style='margin-top:12px'><strong>Filtruoti pagal kategorijas</strong>")
    parts.append("<div class='small'>Pasirinkite kategorijas — rodys tik pažymėtas.</div>")
    parts.append("<div class='filter-controls'><button id='cat_select_all_btn' class='toggle-btn' onclick='toggleSelectAllCats()'>Atžymėti viską</button></div>")
    parts.append("<div style='max-height:220px;overflow:auto;margin-top:8px' class='cat-filter'>")
    for cat in all_categories:
        v = html_lib.escape(cat)
        v_lower = html_lib.escape(cat.lower())
        parts.append(f"<div class='cat-checkbox'><label><input type='checkbox' value='{v_lower}' checked/> <span style='margin-left:6px'>{v}</span></label></div>")
    parts.append("</div></div>")

    parts.append("<ul style='margin-top:10px'>")
    for issuer in issuer_order:
        safe = slugify(issuer)
        cnt = int((df["issuer"] == issuer).sum())
        parts.append(
            f"<li data-issuer='{html_lib.escape(issuer.lower())}'>"
            f"<a href='javascript:void(0)' onclick=\"scrollToId('{safe}')\">"
            f"{html_lib.escape(issuer)} <span class='badge'>{cnt}</span></a></li>"
        )
    parts.append("</ul></aside>")

    parts.append("<main class='content'>")
    summary_txt = ", ".join([f"{r['category']}:{r['count']}" for _, r in summary_df.iterrows()])
    parts.append(
        f"<div class='summary-card'><strong>Santrauka:</strong> &nbsp; "
        f"Visų įrašų skaičius: <strong>{len(df)}</strong> &nbsp; • &nbsp; "
        f"Kategorijų pasiskirstymas: {html_lib.escape(summary_txt)}</div>"
    )

    for issuer in issuer_order:
        issuer_rows = df[df["issuer"] == issuer].copy()
        issuer_rows["date_parsed"] = parse_dates_safe(issuer_rows["date"])
        issuer_rows = issuer_rows.sort_values(by=["date_parsed", "orig_order"], ascending=[False, True])

        safe = slugify(issuer)
        parts.append(f"<section id='{safe}' class='issuer-card'>")
        parts.append("<div class='issuer-header'>")
        parts.append(
            f"<div><div class='issuer-title'>{html_lib.escape(issuer)}</div>"
            f"<div class='small'>{len(issuer_rows)} įrašai</div></div>"
        )
        parts.append(f"<div class='controls'><button class='toggle-btn' data-toggle='#ct_{safe}'>Rodyti</button></div>")
        parts.append("</div>")
        parts.append(f"<div id='ct_{safe}' class='collapsible'>")

        cat_map = defaultdict(list)
        for _, r in issuer_rows.iterrows():
            cats = r.get("categories") or ["Kiti"]
            for c in cats:
                cat_map[c].append(r)

        display_order = CATEGORY_ORDER + [c for c in sorted(cat_map.keys()) if c not in CATEGORY_ORDER]

        for cat in display_order:
            rows = cat_map.get(cat, [])
            parts.append(f"<div class='cat-block'><div class='cat-title'>{html_lib.escape(cat)} <span class='small'>({len(rows)})</span></div>")
            if not rows:
                parts.append("<div class='no-rows'>Nėra įrašų.</div>")
            else:
                parts.append("<table><thead><tr><th class='date-col'>Data</th><th class='title-col'>Antraštė / nuoroda</th><th>Santrauka</th></tr></thead><tbody>")
                for r in rows:
                    date_raw = r.get("date", "")
                    date_fmt = ""
                    dt = pd.to_datetime(date_raw, errors="coerce")
                    if pd.notna(dt):
                        date_fmt = dt.strftime("%Y-%m-%d %H:%M")
                    else:
                        date_fmt = str(date_raw)

                    title_raw = r.get("title", "")
                    url = r.get("url", "")
                    summary_raw = r.get("summary", "")

                    category_from_html = (r.get("category_src") or "").strip()
                    cats_assigned = r.get("categories") or []
                    if isinstance(cats_assigned, str):
                        cats_assigned = [cats_assigned]
                    assigned_display = ", ".join(cats_assigned) if cats_assigned else ""
                    cat_display = category_from_html if category_from_html else assigned_display
                    if not cat_display:
                        cat_display = "Kiti"

                    cats_for_attr = set()
                    if category_from_html:
                        cats_for_attr.add(category_from_html.strip().lower())
                    for cc in cats_assigned:
                        cats_for_attr.add(str(cc).strip().lower())
                    if not cats_for_attr:
                        cats_for_attr.add("kiti")
                    cats_attr = ";".join(sorted([html_lib.escape(c) for c in cats_for_attr]))

                    title_html = highlight_keywords(title_raw)
                    summary_html = highlight_keywords(summary_raw)

                    if url:
                        url_escaped = html_lib.escape(str(url))
                        link_html = f"<a href='{url_escaped}' target='_blank' rel='noreferrer'>{title_html or url_escaped}</a>"
                    else:
                        link_html = title_html or ""

                    link_html = f"{link_html}<div class='small' style='margin-top:6px'>{html_lib.escape(cat_display)}</div>"

                    parts.append(
                        f"<tr data-cats='{cats_attr}'>"
                        f"<td class='date-col'>{html_lib.escape(date_fmt)}</td>"
                        f"<td class='title-col'>{link_html}</td>"
                        f"<td>{summary_html}</td>"
                        f"</tr>"
                    )
                parts.append("</tbody></table>")
            parts.append("</div>")

        parts.append("</div>")
        parts.append("</section>")

    parts.append("</main></div></div>")
    parts.append(f"<script>{js}</script></body></html>")
    return "\n".join(parts)


# ============================================================
# VIESOS FUNKCIJOS STREAMLIT INTEGRACIJAI
# ============================================================


def generate_emitentu_ataskaita(start_date: date, end_date: date, title: Optional[str] = None) -> dict:
    """
    Sugeneruoja emitentų atrankos ataskaita is Supabase.

    Grazina dict:
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
        title = f"Emitentų atranka pagal CRIB naujienas ({start_date} – {end_date})"

    html = build_pretty_html(df, title=title)

    expl = defaultdict(int)
    if not df.empty and "categories" in df.columns:
        for cats in df["categories"]:
            for c in cats:
                expl[c] += 1
    summary = pd.DataFrame(sorted(expl.items(), key=lambda x: -x[1]), columns=["category", "count"])

    return {
        "html": html,
        "df": df,
        "summary": summary,
        "start_date": start_date,
        "end_date": end_date,
    }


def save_emitentu_ataskaita_html(start_date: date, end_date: date, output_path: str | Path) -> Path:
    """Sugeneruoja ir issaugo HTML faila lokaliai."""
    result = generate_emitentu_ataskaita(start_date, end_date)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result["html"], encoding="utf-8")
    return path


def render_emitentu_atranka_page(default_days: int = 30):
    """
    Pilnas Streamlit puslapio blokas. Galima kviesti is app.py.

    Pvz. app.py:
        from emitentu_atranka import render_emitentu_atranka_page
        render_emitentu_atranka_page()
    """
    import streamlit as st
    import streamlit.components.v1 as components

    st.subheader("🧾 Emitentų atranka pagal CRIB naujienas")
    st.caption("Duomenys imami iš Supabase market_news lentelės, source='crib'.")

    today = date.today()
    col1, col2 = st.columns(2)
    with col1:
        start = st.date_input("Nuo", today - timedelta(days=default_days), key="crib_cls_start")
    with col2:
        end = st.date_input("Iki", today, key="crib_cls_end")

    if start > end:
        st.error("Data 'Nuo' negali būti vėlesnė už datą 'Iki'.")
        return

    if st.button("Generuoti klasifikavimo ataskaitą", key="crib_cls_generate"):
        with st.spinner("Kraunamos CRIB naujienos iš duomenų bazės ir generuojama ataskaita..."):
            result = generate_emitentu_ataskaita(start, end)

        st.success(f"Rasta įrašų: {len(result['df'])}")

        if not result["summary"].empty:
            st.dataframe(result["summary"], use_container_width=True, hide_index=True)

        st.download_button(
            "Atsisiųsti HTML ataskaitą",
            data=result["html"].encode("utf-8"),
            file_name=f"crib_klasifikacija_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.html",
            mime="text/html",
        )

        components.html(result["html"], height=900, scrolling=True)


if __name__ == "__main__":
    # Lokalus testas:
    # python emitentu_atranka.py
    res = generate_emitentu_ataskaita(date.today() - timedelta(days=30), date.today())
    out = Path(f"crib_klasifikacija_{date.today().strftime('%Y%m%d')}.html")
    out.write_text(res["html"], encoding="utf-8")
    print(f"Išsaugota: {out.resolve()}")
    print(res["summary"].to_string(index=False) if not res["summary"].empty else "Nėra įrašų.")

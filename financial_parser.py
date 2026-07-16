# -*- coding: utf-8 -*-
"""
financial_parser.py

Nepriklausomas metinių finansinių ataskaitų parseris Rinkos pulsui.

Paskirtis:
- priima PDF / XHTML / HTML / XML / XBRL / ZIP turinį;
- ištraukia žmogui matomas lenteles ir, jei įmanoma, XBRL/iXBRL faktus;
- grąžina normalizuotus faktus:
    metric: assets / equity / revenue / profit / employees_average
    metric_name: Turtas / Nuosavas kapitalas / Pajamos / Grynasis pelnas / Darbuotojų skaičius
    scope: group / company / unspecified
    metric_group: Grupė / Bendrovė / Neatskirta
    period: 2025, 2024, ...
    value: skaičius normalizuotas į tūkst. EUR arba vnt.
    unit: tūkst. EUR / vnt.

Svarbus principas:
- jeigu lentelėje yra stulpeliai „Grupė 2025 2024 Bendrovė 2025 2024“, saugomos abi metų reikšmės;
- finansiniai rodikliai normalizuojami į tūkst. EUR;
- darbuotojai saugomi vienetais.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, asdict
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

try:
    import pdfplumber
except Exception:  # pragma: no cover
    pdfplumber = None

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None

PARSER_VERSION = "financial_parser_2026-07-16a"

LT_TRANS = str.maketrans({
    "ą": "a", "č": "c", "ę": "e", "ė": "e", "į": "i", "š": "s", "ų": "u", "ū": "u", "ž": "z",
    "Ą": "a", "Č": "c", "Ę": "e", "Ė": "e", "Į": "i", "Š": "s", "Ų": "u", "Ū": "u", "Ž": "z",
})

METRIC_LABELS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "assets": (
        "Turtas",
        (
            "turto is viso", "turtas is viso", "turtas iš viso", "turto iš viso",
            "balansinis turtas", "viso turto", "total assets", "assets total", "assets",
        ),
    ),
    "equity": (
        "Nuosavas kapitalas",
        (
            "nuosavo kapitalo is viso", "nuosavas kapitalas is viso", "nuosavo kapitalo iš viso",
            "nuosavas kapitalas", "total equity", "equity total", "equity",
        ),
    ),
    "revenue": (
        "Pajamos",
        (
            "pajamos pagal sutartis su klientais", "pardavimo pajamos", "pajamos is sutarciu", "pajamos iš sutarčių",
            "pajamos", "revenue from contracts with customers", "sales revenue", "revenue", "sales",
        ),
    ),
    "profit": (
        "Grynasis pelnas",
        (
            "grynasis pelnas nuostoliai", "grynasis pelnas", "grynasis nuostolis",
            "laikotarpio pelnas nuostoliai", "metu pelnas", "metų pelnas", "pelnas nuostoliai",
            "profit loss", "profit or loss", "net profit", "net loss", "profit for the year", "loss for the year",
        ),
    ),
    "employees_average": (
        "Darbuotojų skaičius",
        (
            "vidutinis darbuotoju skaicius", "vidutinis darbuotojų skaičius", "darbuotoju skaicius",
            "darbuotojų skaičius", "average number of employees", "number of employees", "employees",
        ),
    ),
}

# XBRL concept aliases. Concept names are normalized before matching.
CONCEPT_ALIASES = {
    "assets": {"assets", "ifrsfullassets", "ifrsfullassetstotal", "totalassets"},
    "equity": {"equity", "ifrsfullequity", "equityattributabletoownersofparent", "totalequity"},
    "revenue": {"revenue", "ifrsfullrevenue", "revenuefromcontractswithcustomers", "salesrevenue"},
    "profit": {"profitloss", "ifrsfullprofitloss", "profitlossattributabletoownersofparent", "netprofitloss"},
    "employees_average": {"averagenumberofemployees", "numberofemployees", "employees"},
}

@dataclass
class Fact:
    metric: str
    metric_name: str
    scope: str
    metric_group: str
    period: Optional[int]
    value: Optional[float]
    unit: str
    source: str
    source_label: str = ""
    source_table: str = ""
    confidence: float = 0.0
    raw_value: str = ""
    unit_original: str = ""
    note: str = ""


def norm_text(value: Any) -> str:
    s = str(value or "").replace("\u00a0", " ")
    s = s.translate(LT_TRANS).lower()
    s = re.sub(r"[–—−]", "-", s)
    s = re.sub(r"[^a-z0-9%.,()\-+/ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def collapse(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def local_name(tag: str) -> str:
    if not tag:
        return ""
    if "}" in tag:
        tag = tag.split("}", 1)[1]
    if ":" in tag:
        tag = tag.split(":")[-1]
    return tag


def concept_key(tag: str) -> str:
    return re.sub(r"[^a-z0-9]", "", norm_text(local_name(tag)))


def clean_number_token(token: Any) -> Tuple[Optional[float], str]:
    if token is None:
        return None, ""
    raw = str(token).strip().replace("\u00a0", " ")
    if not raw or raw.lower() in {"nan", "none", "null", "-", "–", "—"}:
        return None, raw
    neg = bool(re.search(r"\([^)]*\d[^)]*\)", raw)) or raw.lstrip().startswith("-")
    # Neimame metų kaip reikšmės, jei tokenas vien tik metai.
    if re.fullmatch(r"20\d{2}", raw.strip()):
        return None, raw
    m = re.search(r"[-+]?\(?\d[\d\s.,]*\)?", raw)
    if not m:
        return None, raw
    s = m.group(0).replace("(", "").replace(")", "").strip()
    s = s.replace(" ", "")
    # LT ataskaitose 168 483 = 168483, 1,377 mln = 1.377.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # Jei po kablelio 1-2 skaitmenys, tai dešimtainė; jei 3 ir prieš mažai skaitmenų, irgi gali būti 1,377 mln.
        left, right = s.rsplit(",", 1)
        if len(right) <= 3 and len(left) <= 3:
            s = left + "." + right
        else:
            s = s.replace(",", "")
    try:
        v = float(s)
        if neg and v > 0:
            v = -v
        return v, raw
    except Exception:
        return None, raw


def number_tokens_from_cells(cells: Sequence[Any]) -> List[Tuple[float, str, int]]:
    out: List[Tuple[float, str, int]] = []
    for i, cell in enumerate(cells):
        if cell is None:
            continue
        # Vienoje celėje gali būti keli skaičiai, bet dažniau celė = viena reikšmė.
        txt = str(cell).strip()
        if not txt:
            continue
        if re.fullmatch(r"20\d{2}", txt.strip()):
            continue
        val, raw = clean_number_token(txt)
        if val is not None:
            out.append((val, raw, i))
    return out


def years_from_text(text: str, report_year_hint: Optional[int] = None) -> List[int]:
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", str(text or ""))]
    if report_year_hint:
        years.insert(0, int(report_year_hint))
    # Tikėtina finansinių ataskaitų metų seka: naujausi pirmi.
    unique: List[int] = []
    for y in years:
        if 1999 < y < 2100 and y not in unique:
            unique.append(y)
    if not unique and report_year_hint:
        unique = [int(report_year_hint), int(report_year_hint) - 1]
    return unique[:4]


def detect_unit(text: str, metric: str = "") -> Tuple[str, str, float, str]:
    """Returns normalized unit, original unit category, multiplier to tūkst. EUR/vnt, note."""
    if metric == "employees_average":
        return "vnt.", "vnt.", 1.0, "darbuotojų rodiklis saugomas vienetais"
    t = norm_text(text)
    # million first, because text may also contain EUR.
    if re.search(r"\b(mln|million|millions)\b", t):
        return "tūkst. EUR", "mln. EUR", 1000.0, "mln. EUR konvertuota į tūkst. EUR"
    if re.search(r"\b(tukst|thousand|thousands|keur)\b", t):
        return "tūkst. EUR", "tūkst. EUR", 1.0, "reikšmės jau pateiktos tūkst. EUR"
    if re.search(r"\b(eur|euro|euros)\b", t):
        return "tūkst. EUR", "EUR", 0.001, "EUR konvertuota į tūkst. EUR"
    return "tūkst. EUR", "neaišku", 1.0, "vienetas neatpažintas, palikta kaip tūkst. EUR"


def metric_from_label(label: str) -> Optional[str]:
    t = norm_text(label)
    if not t:
        return None
    for metric, (_name, labels) in METRIC_LABELS.items():
        for lab in labels:
            lab_n = norm_text(lab)
            if lab_n and lab_n in t:
                # saugiklis: „pajamos“ kartais atsiranda pastabų tekste, bet lentelėje tinka
                return metric
    return None


def scope_group_name(scope: str) -> str:
    return {"group": "Grupė", "company": "Bendrovė"}.get(scope, "Neatskirta")


def scope_from_context(text: str) -> Optional[str]:
    t = norm_text(text)
    if re.search(r"\b(grupe|group|consolidated|konsoliduot)\b", t):
        return "group"
    if re.search(r"\b(bendrove|company|separate|parent|individual|atskiros)\b", t):
        return "company"
    return None


def map_values_to_periods_and_scopes(
    values: List[Tuple[float, str, int]],
    context_text: str,
    metric: str,
    report_year_hint: Optional[int],
) -> List[Tuple[str, Optional[int], float, str, str, float]]:
    """Map row numbers to (scope, year, value, raw, unit_original, multiplier)."""
    if not values:
        return []
    years = years_from_text(context_text, report_year_hint)
    unit, unit_orig, mult, unit_note = detect_unit(context_text, metric)
    nums = [(v * mult, raw) for v, raw, _idx in values]
    t = norm_text(context_text)
    has_group = bool(re.search(r"\b(grupe|group|konsoliduot|consolidated)\b", t))
    has_company = bool(re.search(r"\b(bendrove|company|separate|parent|individual|atskiros)\b", t))
    out: List[Tuple[str, Optional[int], float, str, str, float]] = []

    # Tipinė Baltijos metinių ataskaitų struktūra:
    # Grupė 2025 2024 Bendrovė 2025 2024 -> 4 skaičiai.
    if has_group and has_company and len(nums) >= 4:
        n_years = 2
        if len(nums) >= 6 and len(years) >= 3:
            n_years = 3
        elif len(years) >= 2:
            n_years = 2
        else:
            n_years = min(2, len(nums) // 2)
        use_years = years[:n_years] if years else [report_year_hint, None][:n_years]
        for i, yr in enumerate(use_years):
            if i < len(nums):
                out.append(("group", yr, nums[i][0], nums[i][1], unit_orig, 0.96))
        offset = n_years
        for i, yr in enumerate(use_years):
            if offset + i < len(nums):
                out.append(("company", yr, nums[offset + i][0], nums[offset + i][1], unit_orig, 0.96))
        return out

    # Viena apimtis ir keli metai.
    one_scope = scope_from_context(context_text)
    if one_scope and len(nums) >= 2 and years:
        for i, yr in enumerate(years[:len(nums)]):
            out.append((one_scope, yr, nums[i][0], nums[i][1], unit_orig, 0.86))
        return out

    # Jei yra tik 2 skaičiai ir kontekste aiškiai minima grupė/bendrovė, bet ne abu.
    if len(nums) >= 2 and years:
        for i, yr in enumerate(years[:min(len(nums), 2)]):
            out.append(("unspecified", yr, nums[i][0], nums[i][1], unit_orig, 0.65))
        return out

    # Vienas skaičius.
    yr = years[0] if years else report_year_hint
    out.append((one_scope or "unspecified", yr, nums[0][0], nums[0][1], unit_orig, 0.55))
    return out


def fact_from_row(row: Sequence[Any], table_context: str, source: str, source_label: str, report_year_hint: Optional[int]) -> List[Fact]:
    row_text = " ".join(collapse(c) for c in row if collapse(c))
    metric = metric_from_label(row_text)
    if not metric:
        return []
    vals = number_tokens_from_cells(row)
    # Jeigu labelio celėje yra pastabos numeris, dažnai tai pirmas skaičius. Pašaliname mažus pastabų numerius,
    # kai eilutėje yra daugiau nei vienas skaičius.
    if len(vals) > 1:
        filtered = []
        for v, raw, idx in vals:
            if idx <= 2 and abs(v) < 100 and metric != "employees_average":
                continue
            filtered.append((v, raw, idx))
        if filtered:
            vals = filtered
    context = f"{table_context}\n{row_text}"
    mapped = map_values_to_periods_and_scopes(vals, context, metric, report_year_hint)
    metric_name = METRIC_LABELS[metric][0]
    unit, _, _, unit_note = detect_unit(context, metric)
    facts: List[Fact] = []
    for scope, period, value, raw, unit_orig, conf in mapped:
        # Finansinių rodiklių saugiklis: labai mažos reikšmės dažnai yra pastabos / xbrl artefaktai.
        if metric != "employees_average" and abs(value) < 1:
            continue
        facts.append(Fact(
            metric=metric,
            metric_name=metric_name,
            scope=scope,
            metric_group=scope_group_name(scope),
            period=period,
            value=round(value, 3) if value is not None else None,
            unit=unit,
            source=source,
            source_label=source_label,
            source_table=table_context[:500],
            confidence=conf,
            raw_value=raw,
            unit_original=unit_orig,
            note=unit_note,
        ))
    return facts


def dedupe_facts(facts: Iterable[Fact]) -> List[Fact]:
    best: Dict[Tuple[str, str, Optional[int]], Fact] = {}
    for f in facts:
        if f.value is None:
            continue
        key = (f.metric, f.scope, f.period)
        old = best.get(key)
        if old is None or (f.confidence, len(f.source_table)) > (old.confidence, len(old.source_table)):
            best[key] = f
    return sorted(best.values(), key=lambda f: (f.metric, f.scope, -(f.period or 0)))


def extract_pdf(content: bytes, report_year_hint: Optional[int] = None, source: str = "pdf") -> Tuple[List[Fact], str, Dict[str, Any]]:
    if pdfplumber is None:
        return [], "", {"error": "pdfplumber not installed"}
    text_parts: List[str] = []
    facts: List[Fact] = []
    tables_count = 0
    with pdfplumber.open(BytesIO(content)) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            try:
                page_text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            except Exception:
                page_text = page.extract_text() or ""
            if page_text:
                text_parts.append(page_text)
            tables = []
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            for table in tables:
                tables_count += 1
                header_rows = table[:4] if table else []
                ctx = page_text[:2000] + "\n" + "\n".join(" | ".join(collapse(c) for c in r) for r in header_rows)
                for row in table:
                    facts.extend(fact_from_row(row or [], ctx, source, f"PDF p.{pageno}", report_year_hint))
    raw_text = "\n".join(text_parts)
    if not facts and raw_text:
        facts.extend(extract_from_text_lines(raw_text, report_year_hint, source=source, source_label="PDF text"))
    return dedupe_facts(facts), raw_text[:200000], {"pdf_tables": tables_count, "raw_text_len": len(raw_text)}


def extract_html(content: bytes, report_year_hint: Optional[int] = None, source: str = "html") -> Tuple[List[Fact], str, Dict[str, Any]]:
    if BeautifulSoup is None:
        return [], "", {"error": "beautifulsoup not installed"}
    html = content.decode("utf-8", errors="ignore") if isinstance(content, (bytes, bytearray)) else str(content)
    soup = BeautifulSoup(html, "html.parser")
    raw_text = soup.get_text("\n", strip=True)
    facts: List[Fact] = []
    tables_count = 0
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if not rows:
            continue
        tables_count += 1
        ctx = "\n".join(" | ".join(r) for r in rows[:5])
        for row in rows:
            facts.extend(fact_from_row(row, ctx, source, "HTML table", report_year_hint))
    # Daug iXBRL dokumentų neturi tvarkingų table tagų arba tekstas išbarstytas. Tada naudojame eilučių parserį.
    line_facts = extract_from_text_lines(raw_text, report_year_hint, source=source, source_label="HTML text")
    facts.extend(line_facts)
    # iXBRL faktai kaip papildomas šaltinis, bet ne aukštesnio prioriteto už matomą lentelę.
    facts.extend(extract_xbrl_facts_from_soup(soup, raw_text, report_year_hint, source=source))
    return dedupe_facts(facts), raw_text[:200000], {"html_tables": tables_count, "raw_text_len": len(raw_text)}


def extract_from_text_lines(text: str, report_year_hint: Optional[int] = None, source: str = "text", source_label: str = "text") -> List[Fact]:
    lines = [collapse(x) for x in str(text or "").splitlines() if collapse(x)]
    facts: List[Fact] = []
    for i, line in enumerate(lines):
        if not metric_from_label(line):
            continue
        window = "\n".join(lines[max(0, i - 8): min(len(lines), i + 4)])
        # Bandom sukonstruoti vieną eilutę: label + po jo einantys skaičiai, jei XHTML išbarstė celės po eilutę.
        row_cells = [line]
        for nxt in lines[i + 1:i + 10]:
            if metric_from_label(nxt):
                break
            if re.search(r"\d", nxt):
                row_cells.append(nxt)
            if len(number_tokens_from_cells(row_cells)) >= 6:
                break
        facts.extend(fact_from_row(row_cells, window, source, source_label, report_year_hint))
    return dedupe_facts(facts)


def extract_xbrl_facts_from_soup(soup: Any, raw_text: str, report_year_hint: Optional[int], source: str = "xbrl") -> List[Fact]:
    facts: List[Fact] = []
    # ix:nonFraction tags can be represented as 'ix:nonfraction' by bs4.
    candidates = []
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        if name.endswith("nonfraction") or "nonfraction" in name:
            candidates.append(tag)
    contexts_text = raw_text[:5000]
    for tag in candidates:
        concept = tag.get("name") or tag.get("contextref") or ""
        key = concept_key(str(concept))
        metric = None
        for m, aliases in CONCEPT_ALIASES.items():
            if key in aliases or any(a in key for a in aliases):
                metric = m
                break
        if not metric:
            continue
        val, raw = clean_number_token(tag.get_text(" ", strip=True))
        if val is None:
            continue
        scale = tag.get("scale")
        try:
            if scale is not None and str(scale).strip():
                val = val * (10 ** int(scale))
        except Exception:
            pass
        unit, unit_orig, mult, unit_note = detect_unit(str(tag.get("unitref") or "") + " " + contexts_text, metric)
        if metric != "employees_average":
            val = val * mult
        period = None
        # contextRef dažnai turi metus.
        ctx = str(tag.get("contextref") or tag.get("contextRef") or "")
        ys = years_from_text(ctx + " " + contexts_text, report_year_hint)
        if ys:
            period = ys[0]
        scope = scope_from_context(ctx) or "unspecified"
        facts.append(Fact(
            metric=metric,
            metric_name=METRIC_LABELS[metric][0],
            scope=scope,
            metric_group=scope_group_name(scope),
            period=period,
            value=round(val, 3),
            unit=unit,
            source=source,
            source_label="iXBRL fact",
            confidence=0.5,
            raw_value=raw,
            unit_original=unit_orig,
            note=unit_note,
        ))
    return facts


def extract_xml(content: bytes, report_year_hint: Optional[int] = None, source: str = "xml") -> Tuple[List[Fact], str, Dict[str, Any]]:
    text = content.decode("utf-8", errors="ignore") if isinstance(content, (bytes, bytearray)) else str(content)
    # Jei tai XHTML/iXBRL, geriau per HTML parserį.
    if "<html" in text[:2000].lower() or "nonfraction" in text.lower():
        return extract_html(content, report_year_hint, source=source)
    facts: List[Fact] = []
    try:
        root = ET.fromstring(content)
    except Exception:
        return [], text[:200000], {"error": "xml parse error", "raw_text_len": len(text)}
    contexts = {}
    for elem in root.iter():
        if local_name(elem.tag).lower() == "context":
            cid = elem.attrib.get("id") or ""
            if cid:
                contexts[cid] = " ".join(elem.itertext())
    for elem in root.iter():
        key = concept_key(elem.tag)
        metric = None
        for m, aliases in CONCEPT_ALIASES.items():
            if key in aliases or any(a == key or a in key for a in aliases):
                metric = m
                break
        if not metric:
            continue
        val, raw = clean_number_token(elem.text)
        if val is None:
            continue
        ctx = elem.attrib.get("contextRef") or elem.attrib.get("contextref") or ""
        ctx_text = ctx + " " + contexts.get(ctx, "")
        unit, unit_orig, mult, unit_note = detect_unit(str(elem.attrib.get("unitRef") or elem.attrib.get("unitref") or "") + " EUR", metric)
        if metric != "employees_average":
            val = val * mult
        ys = years_from_text(ctx_text, report_year_hint)
        scope = scope_from_context(ctx_text) or "unspecified"
        facts.append(Fact(
            metric=metric,
            metric_name=METRIC_LABELS[metric][0],
            scope=scope,
            metric_group=scope_group_name(scope),
            period=ys[0] if ys else report_year_hint,
            value=round(val, 3),
            unit=unit,
            source=source,
            source_label="XBRL fact",
            confidence=0.55,
            raw_value=raw,
            unit_original=unit_orig,
            note=unit_note,
        ))
    return dedupe_facts(facts), text[:200000], {"xbrl_facts": len(facts), "raw_text_len": len(text)}


def guess_file_type(name: str, content: bytes = b"") -> str:
    n = str(name or "").lower()
    sample = (content or b"")[:1000].lower()
    if (content or b"")[:4] == b"PK\x03\x04" or n.endswith(".zip"):
        return "zip"
    if sample.lstrip().startswith(b"%pdf") or n.endswith(".pdf"):
        return "pdf"
    if n.endswith((".xhtml", ".html", ".htm")) or b"<html" in sample or b"nonfraction" in sample:
        return "html"
    if n.endswith((".xml", ".xbrl")) or sample.lstrip().startswith(b"<?xml"):
        return "xml"
    return "text"


def zip_members(content: bytes) -> List[Tuple[str, bytes, str]]:
    out: List[Tuple[str, bytes, str]] = []
    with zipfile.ZipFile(BytesIO(content)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        def score(info):
            n = info.filename.lower().replace("\\", "/")
            if "meta-inf" in n or n.endswith(".xsd") or any(x in n for x in ["_lab", "_pre", "_cal", "_def", "catalog", "taxonomypackage"]):
                return 99
            if n.startswith("reports/") and n.endswith((".xhtml", ".html", ".htm")):
                return 0
            if n.endswith((".xhtml", ".html", ".htm")):
                return 1
            if n.endswith((".xbrl", ".xml")):
                return 2
            if n.endswith(".pdf"):
                return 3 - min(info.file_size, 10_000_000) / 10_000_000
            return 50
        for info in sorted(infos, key=score):
            if score(info) >= 50:
                continue
            try:
                data = zf.read(info)
                out.append((info.filename, data, guess_file_type(info.filename, data)))
            except Exception:
                continue
    return out


def parse_financial_document(
    content: bytes,
    file_type: str = "",
    file_name: str = "",
    report_year_hint: Optional[int] = None,
) -> Dict[str, Any]:
    ftype = (file_type or "").lower().strip() or guess_file_type(file_name, content)
    if ftype in {"xhtml", "htm"}:
        ftype = "html"
    diagnostics: Dict[str, Any] = {"parser_version": PARSER_VERSION, "file_type": ftype, "file_name": file_name}
    facts: List[Fact] = []
    raw_text = ""
    if ftype == "zip":
        members = zip_members(content)
        diagnostics["zip_members_selected"] = len(members)
        raw_parts = []
        for name, data, mt in members:
            parsed = parse_financial_document(data, mt, name, report_year_hint)
            raw_parts.append(f"\n--- ZIP ENTRY: {name} ---\n" + str(parsed.get("raw_text") or "")[:60000])
            for fd in parsed.get("facts", []):
                fd = dict(fd)
                fd["source"] = "zip_" + str(fd.get("source") or mt)
                fd["source_label"] = name
                facts.append(Fact(**{k: fd.get(k) for k in Fact.__dataclass_fields__.keys()}))
        raw_text = "\n".join(raw_parts)[:200000]
        diagnostics["raw_text_len"] = len(raw_text)
        return {"facts": [asdict(f) for f in dedupe_facts(facts)], "raw_text": raw_text, "diagnostics": diagnostics}
    if ftype == "pdf":
        facts, raw_text, diag = extract_pdf(content, report_year_hint, source="pdf")
        diagnostics.update(diag)
    elif ftype in {"html", "xhtml"}:
        facts, raw_text, diag = extract_html(content, report_year_hint, source="html")
        diagnostics.update(diag)
    elif ftype in {"xml", "xbrl"}:
        facts, raw_text, diag = extract_xml(content, report_year_hint, source="xbrl")
        diagnostics.update(diag)
    else:
        text = content.decode("utf-8", errors="ignore") if isinstance(content, (bytes, bytearray)) else str(content or "")
        raw_text = text
        facts = extract_from_text_lines(text, report_year_hint, source="text", source_label="text")
        diagnostics["raw_text_len"] = len(raw_text)
    diagnostics["facts_found"] = len(facts)
    return {"facts": [asdict(f) for f in dedupe_facts(facts)], "raw_text": raw_text[:200000], "diagnostics": diagnostics}


def facts_to_annual_report_metrics_dict(facts: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str, Optional[int]], Dict[str, Any]]:
    """Adapteris senam metines.py formatui."""
    out: Dict[Tuple[str, str, Optional[int]], Dict[str, Any]] = {}
    for f in facts or []:
        metric_name = f.get("metric_name") or f.get("metric")
        metric_group = f.get("metric_group") or scope_group_name(f.get("scope") or "unspecified")
        year = f.get("period")
        try:
            year = int(year) if year is not None and str(year) != "" else None
        except Exception:
            year = None
        key = (metric_name, metric_group, year)
        val = f.get("value")
        if val is None:
            continue
        candidate = {
            "value": val,
            "unit": f.get("unit") or "tūkst. EUR",
            "source_type": f.get("source") or "parser",
            "source_label": f.get("source_label") or "",
            "confidence": float(f.get("confidence") or 0),
            "parse_status": "parsed_by_financial_parser",
            "parse_note": f"{f.get('note') or ''}; original unit: {f.get('unit_original') or ''}; raw: {f.get('raw_value') or ''}".strip("; "),
            "raw_text": f.get("source_table") or "",
        }
        old = out.get(key)
        if old is None or candidate["confidence"] > float(old.get("confidence") or 0):
            out[key] = candidate
    return out

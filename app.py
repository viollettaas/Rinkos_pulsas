# -*- coding: utf-8 -*-
from datetime import date, timedelta
from io import BytesIO
import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from rinkos_logika import (
    generate_report,
    extract_dates_from_filename,
    download_nasdaq_statistics_excel,
    atitinka,
    extract_date_from_url,
    _open_new_tab_and_get,
)
from emitentu_atranka import generate_emitentu_ataskaita
from crib_update import update_crib_news, get_latest_crib_news_date
from supabase_cache import save_news_df, load_news_df
from issuer_cache import save_issuer_list_from_stat_df, load_issuer_df

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
try:
    from vadovu_sandoriai import show_manager_transactions_page
except Exception:
    show_manager_transactions_page = None


st.set_page_config(
    page_title="Rinkos pulsas",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)


if "report_result" not in st.session_state:
    st.session_state.report_result = None

if "report_filename" not in st.session_state:
    st.session_state.report_filename = None

if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 1

if "uploaded_file_cache" not in st.session_state:
    st.session_state.uploaded_file_cache = None

if "emitentu_result" not in st.session_state:
    st.session_state.emitentu_result = None

if "emitentu_dates" not in st.session_state:
    st.session_state.emitentu_dates = None

if "news_update_message" not in st.session_state:
    st.session_state.news_update_message = None


CSS = """
<style>
.stApp {
    background: #ffffff;
}

.block-container {
    padding-top: 1.2rem;
    padding-left: 2rem;
    padding-right: 2rem;
    max-width: 100% !important;
}

/* SIDEBAR */
section[data-testid="stSidebar"] {
    background: radial-gradient(circle at top left, #0c356b 0%, #061d3a 35%, #03162d 100%) !important;
    min-width: 350px !important;
    max-width: 350px !important;
}

section[data-testid="stSidebar"] > div {
    padding: 14px 18px 22px 18px;
}

section[data-testid="stSidebar"] * {
    color: #ffffff;
}

.sidebar-title {
    font-size: 20px;
    font-weight: 900;
    margin-bottom: 12px;
}

.sidebar-card {
    background: rgba(255,255,255,0.055);
    border: 1px solid rgba(157, 190, 230, 0.28);
    border-radius: 17px;
    padding: 20px 16px;
    box-shadow: 0 18px 45px rgba(0,0,0,0.24);
    margin-bottom: 22px;
}

.sidebar-card-title {
    font-size: 16px;
    font-weight: 900;
    margin-bottom: 10px;
}

.sidebar-card-subtitle {
    color: #b8c9df !important;
    font-size: 13px;
    margin-bottom: 16px;
}

.upload-area {
    background: rgba(2, 19, 42, 0.48);
    border: 1.5px dashed #1478ff;
    border-radius: 16px;
    padding: 22px 16px;
    min-height: 170px;
}

/* File uploader */
section[data-testid="stSidebar"] [data-testid="stFileUploader"] {
    background: transparent !important;
    border: none !important;
    padding: 0 !important;
}

section[data-testid="stSidebar"] [data-testid="stFileUploader"] section {
    background: transparent !important;
    border: none !important;
    padding: 0 !important;
}

section[data-testid="stSidebar"] [data-testid="stFileUploader"] section > div {
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;
    text-align: center !important;
}

section[data-testid="stSidebar"] [data-testid="stFileUploader"] button,
section[data-testid="stSidebar"] .stButton > button {
    background: linear-gradient(135deg, #1478ff, #0066ff) !important;
    color: white !important;
    border: none !important;
    border-radius: 12px !important;
    height: 48px !important;
    font-weight: 900 !important;
    box-shadow: 0 12px 26px rgba(20,120,255,0.35);
}

section[data-testid="stSidebar"] [data-testid="stFileUploader"] small,
section[data-testid="stSidebar"] [data-testid="stFileUploader"] span,
section[data-testid="stSidebar"] [data-testid="stFileUploader"] p {
    color: #a9bad3 !important;
    text-align: center !important;
}

/* Radio */
section[data-testid="stSidebar"] div[role="radiogroup"] label {
    background: rgba(255,255,255,0.07);
    border: 1px solid rgba(157,190,230,0.22);
    border-radius: 12px;
    padding: 8px 10px;
    margin-bottom: 8px;
}

/* File card */
.file-status-card {
    margin-top: 10px;
    background: rgba(255,255,255,0.075);
    border: 1px solid rgba(157,190,230,0.24);
    border-radius: 14px;
    padding: 13px 14px;
}

.file-status-row {
    display: flex;
    align-items: center;
    gap: 12px;
}

.file-icon {
    width: 36px;
    height: 36px;
    border-radius: 10px;
    background: rgba(20,120,255,0.18);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 19px;
}

.file-main {
    flex: 1;
    min-width: 0;
}

.file-name {
    color: #ffffff !important;
    font-size: 13px;
    font-weight: 800;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.file-meta {
    color: #a9bad3 !important;
    font-size: 12px;
    margin-top: 2px;
}

.file-ok {
    color: #23d996 !important;
    font-size: 18px;
    font-weight: 900;
}

.status-ok {
    color: #23d996 !important;
    font-weight: 800;
    margin-top: 12px;
    font-size: 13px;
}

.status-empty {
    color: #c1cee0 !important;
    font-weight: 700;
    margin-top: 12px;
    font-size: 13px;
}

.latest-news-date {
    margin-top: 12px;
    background: rgba(255,255,255,0.07);
    border: 1px solid rgba(157,190,230,0.22);
    border-radius: 12px;
    padding: 10px 12px;
    color: #cfe2ff !important;
    font-size: 13px;
    font-weight: 700;
}

.latest-news-date span {
    color: #ffffff !important;
    font-weight: 900;
}

/* Inputs */
section[data-testid="stSidebar"] label {
    color: white !important;
    font-weight: 700 !important;
}

section[data-testid="stSidebar"] input {
    color: white !important;
    background: rgba(255,255,255,0.07) !important;
}

section[data-testid="stSidebar"] .stDateInput div[data-baseweb="input"] {
    background: rgba(255,255,255,0.07) !important;
    border: 1px solid rgba(157,190,230,0.22) !important;
    border-radius: 12px !important;
}

/* Hero */
.hero-card {
    background: linear-gradient(135deg, #ffffff 0%, #f2f8ff 52%, #dcecff 100%);
    border: 1px solid #dbe7f5;
    border-radius: 18px;
    padding: 28px 32px;
    min-height: 172px;
    box-shadow: 0 8px 28px rgba(8, 44, 84, 0.08);
    position: relative;
    overflow: hidden;
}

.hero-card::after {
    content: "";
    position: absolute;
    right: 0;
    bottom: 0;
    width: 370px;
    height: 95px;
    background: rgba(20,120,255,0.16);
    clip-path: polygon(0 100%, 36% 60%, 55% 68%, 73% 32%, 87% 46%, 100% 22%, 100% 100%);
}

.hero-inner {
    display: flex;
    align-items: flex-start;
    gap: 18px;
    position: relative;
    z-index: 2;
}

.hero-icon {
    width: 58px;
    height: 58px;
    border-radius: 14px;
    background: #e3efff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 30px;
}

.hero-title {
    font-size: 36px;
    line-height: 1.05;
    font-weight: 950;
    color: #071f3d;
    margin: 0;
}

.hero-text {
    color: #34435a;
    margin-top: 8px;
    font-size: 15px;
    max-width: 660px;
}

.hero-download button {
    background: #061b34 !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    min-height: 52px !important;
    font-weight: 900 !important;
}

/* Metrics */
div[data-testid="metric-container"] {
    background: white;
    border: 1px solid #dbe7f5;
    border-radius: 16px;
    padding: 18px 20px;
    min-height: 105px;
    box-shadow: 0 8px 25px rgba(8, 44, 84, 0.07);
}

div[data-testid="metric-container"] label {
    color: #334155 !important;
    font-weight: 800 !important;
}

div[data-testid="metric-container"] div {
    color: #0f172a !important;
}

/* Tabs */
.stTabs [data-baseweb="tab-list"] {
    gap: 18px;
    border-bottom: 1px solid #dde8f4;
}

.stTabs [data-baseweb="tab"] {
    padding: 16px 8px 14px 8px;
    font-weight: 800;
    color: #223044;
    background: transparent;
}

.stTabs [aria-selected="true"] {
    color: #1478ff !important;
    border-bottom: 3px solid #1478ff !important;
}

/* Tables */
.report-table-wrapper {
    background: white;
    border: 1px solid #dbe7f5;
    border-radius: 16px;
    padding: 14px;
    margin-top: 16px;
    box-shadow: 0 10px 28px rgba(8, 44, 84, 0.08);
    overflow-x: auto;
}

.report-table-wrapper table {
    width: 100%;
    border-collapse: collapse;
}

.report-table-wrapper caption {
    caption-side: top;
    text-align: left;
    font-weight: 950;
    font-size: 20px;
    color: #071f3d;
    padding: 12px 0 18px 0;
}

.report-table-wrapper th {
    background: #061b34 !important;
    color: white !important;
    padding: 12px 10px !important;
    font-size: 12px !important;
    text-align: left !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
}

.report-table-wrapper td {
    padding: 10px !important;
    font-size: 12px !important;
    color: #102033 !important;
    border-bottom: 1px solid #e7eef7 !important;
}

.report-table-wrapper tr:nth-child(even) td {
    background: #f8fbff !important;
}

.report-table-wrapper tr:hover td {
    background: #eaf3ff !important;
}

.info-box {
    background: #eaf3ff;
    color: #0b3f77;
    border: 1px solid #c9dff8;
    border-radius: 14px;
    padding: 18px 20px;
    margin-top: 24px;
    font-weight: 700;
}


/* Custom report navigation */
.report-nav-title {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 25px;
    font-weight: 950;
    margin: 0 0 14px 0;
    letter-spacing: -0.2px;
}
.report-nav-icon {
    width: 38px;
    height: 38px;
    border-radius: 11px;
    background: rgba(255,255,255,0.16);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.18);
    font-size: 21px;
}
.report-nav {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-bottom: 18px;
}
.report-nav a { text-decoration: none !important; }
.report-nav-item {
    display: flex;
    align-items: center;
    gap: 14px;
    min-height: 56px;
    padding: 0 18px;
    border-radius: 16px;
    background: rgba(255,255,255,0.055);
    border: 1px solid rgba(157,190,230,0.26);
    color: #ffffff !important;
    font-weight: 900;
    font-size: 16px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.12);
    transition: all .18s ease;
}
.report-nav-item:hover {
    background: rgba(30,120,220,0.18);
    border-color: rgba(111,190,255,0.62);
    transform: translateY(-1px);
}
.report-nav-item.active {
    background: rgba(20,120,255,0.14);
    border: 3px solid #68bdff;
    box-shadow: 0 0 0 1px rgba(104,189,255,0.18), 0 0 22px rgba(104,189,255,0.26);
}
.report-nav-item .nav-icon {
    font-size: 24px;
    width: 28px;
    text-align: center;
    opacity: .95;
}
.report-nav-item.active .nav-icon { color: #68bdff !important; }
section[data-testid="stSidebar"] div[role="radiogroup"] label > div:first-child,
section[data-testid="stSidebar"] div[role="radiogroup"] input[type="radio"] { display: none !important; }


/* Kai šoninis meniu suskleistas - pagrindinė ataskaita išsiplečia per visą langą */
section[data-testid="stSidebar"][aria-expanded="false"] {
    min-width: 0rem !important;
    max-width: 0rem !important;
    width: 0rem !important;
    transform: translateX(-100%) !important;
    overflow: hidden !important;
}

section[data-testid="stSidebar"][aria-expanded="false"] > div {
    display: none !important;
    width: 0rem !important;
    min-width: 0rem !important;
    max-width: 0rem !important;
    padding: 0 !important;
}

[data-testid="stSidebarCollapsedControl"] {
    left: 0.8rem !important;
    top: 0.8rem !important;
    z-index: 999999 !important;
}

[data-testid="stAppViewContainer"] {
    width: 100% !important;
    max-width: 100% !important;
}

[data-testid="stAppViewContainer"] .main,
[data-testid="stAppViewContainer"] section.main {
    width: 100% !important;
    max-width: 100% !important;
}

[data-testid="stAppViewContainer"] .block-container {
    max-width: 100% !important;
}

.report-table-wrapper,
.report-table-wrapper table {
    width: 100% !important;
    max-width: 100% !important;
}


.update-card {
    margin-top: 8px;
}

.update-card .stButton > button {
    background: rgba(59, 130, 246, 0.18) !important;
    color: #ffffff !important;
    border: 1px solid rgba(96, 165, 250, 0.55) !important;
    border-radius: 14px !important;
    height: 46px !important;
    font-weight: 900 !important;
    box-shadow: 0 0 0 1px rgba(147, 197, 253, 0.14), 0 8px 20px rgba(0,0,0,0.16) !important;
}

.update-card .stButton > button:hover {
    border-color: #7dd3fc !important;
    box-shadow: 0 0 0 2px rgba(125, 211, 252, 0.25), 0 12px 26px rgba(0,0,0,0.22) !important;
}

.sidebar-section-title {
    font-size: 16px;
    font-weight: 950;
    margin: 16px 0 8px 0;
    letter-spacing: -0.1px;
}
.sidebar-section-subtitle {
    color: #b8c9df !important;
    font-size: 13px;
    line-height: 1.45;
    margin: 0 0 12px 0;
}
.news-db-block {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid rgba(157,190,230,0.20);
}
.datasource-block {
    margin-top: 16px;
}
section[data-testid="stSidebar"] .stRadio {
    margin-bottom: 8px !important;
}
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
    gap: 0.45rem !important;
}

</style>
"""

st.markdown(CSS, unsafe_allow_html=True)


def format_size(size_bytes: int) -> str:
    if size_bytes is None:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def download_nasdaq_statistics_df_for_vz(start_date: date, end_date: date) -> pd.DataFrame:
    """
    Atsisiunčia Nasdaq Baltic statistics Excel pagal pasirinktą laikotarpį
    ir grąžina VLN lapą kaip DataFrame. Naudojama tik emitentų sąrašui,
    pagal kurį atrenkami aktualūs VŽ straipsniai.
    """
    if start_date is None or end_date is None:
        raise ValueError("VŽ atnaujinimui reikia nurodyti pradžios ir pabaigos datas.")
    if start_date > end_date:
        raise ValueError("Data „Nuo“ negali būti vėlesnė už datą „Iki“.")

    base_url = "https://nasdaqbaltic.com/statistics/lt/statistics/download"
    params = {
        "filter": 1,
        "start": start_date.strftime("%Y-%m-%d"),
        "end": end_date.strftime("%Y-%m-%d"),
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
            "application/vnd.ms-excel,application/octet-stream,*/*"
        ),
        "Referer": "https://nasdaqbaltic.com/statistics/lt/statistics",
        "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    response = requests.get(
        base_url,
        params=params,
        headers=headers,
        verify=False,
        timeout=60,
    )
    response.raise_for_status()

    content = response.content or b""
    content_type = response.headers.get("Content-Type", "").lower()
    if content[:100].lstrip().lower().startswith(b"<html") or "text/html" in content_type:
        raise RuntimeError(
            "Nasdaq Baltic vietoje Excel grąžino HTML puslapį. "
            "Pabandykite dar kartą arba naudokite jau sugeneruotos ataskaitos emitentų sąrašą."
        )

    excel_file = BytesIO(content)
    try:
        return pd.read_excel(excel_file, sheet_name="VLN")
    except Exception:
        excel_file.seek(0)
        return pd.read_excel(excel_file)



def _init_vz_driver(headless: bool = True):
    """Selenium driveris VŽ pirmo puslapio patikrinimui.
    Naudojamas Selenium Manager, kad Streamlit Cloud nekiltų ChromeDriver versijų konfliktų.
    """
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1600,1200")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=lt")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors=yes")
    options.set_capability("acceptInsecureCerts", True)
    return webdriver.Chrome(options=options)


def _existing_vz_urls() -> set:
    """Pasiima jau DB esančių VŽ URL sąrašą, kad neatidarinėtume straipsnių pakartotinai."""
    try:
        df_existing = load_news_df("vz", date(2023, 1, 1), date.today())
    except Exception:
        return set()

    if df_existing is None or df_existing.empty:
        return set()

    url_col = "url" if "url" in df_existing.columns else "Nuoroda" if "Nuoroda" in df_existing.columns else None
    if url_col is None:
        return set()

    return {
        str(u).strip().lower()
        for u in df_existing[url_col].dropna().tolist()
        if str(u).strip()
    }


def _title_matches_issuers(title: str, issuers) -> bool:
    if not title:
        return False
    for issuer in issuers:
        if atitinka(str(issuer), str(title)):
            return True
    return False


def vz_scrape_first_page_fast(df_stat: pd.DataFrame, progress=None) -> pd.DataFrame:
    """Greitas VŽ atnaujinimas.

    Skaito tik pirmą VŽ puslapį. Pilną straipsnio tekstą atidaro tik tada, kai:
    1) straipsnio URL dar nėra DB;
    2) antraštė atitinka bent vieną emitentą iš df_stat["Bendrovė"].

    Grąžina DataFrame, suderinamą su save_news_df(..., "vz").
    """
    if df_stat is None or df_stat.empty or "Bendrovė" not in df_stat.columns:
        return pd.DataFrame(columns=["Antraštė", "Nuoroda", "Data", "Pilnas_tekstas"])

    issuers = [str(x).strip() for x in df_stat["Bendrovė"].dropna().unique().tolist() if str(x).strip()]
    if not issuers:
        return pd.DataFrame(columns=["Antraštė", "Nuoroda", "Data", "Pilnas_tekstas"])

    existing_urls = _existing_vz_urls()
    driver = _init_vz_driver(headless=True)
    items = []

    try:
        driver.get("https://www.vz.lt/")
        try:
            WebDriverWait(driver, 12).until(lambda d: d.execute_script("return document.readyState") == "complete")
        except Exception:
            pass

        # Slapukai, jei rodomi.
        for by, selector in [
            (By.ID, "CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"),
            (By.XPATH, "//button[contains(., 'Sutinku') or contains(., 'Priimti') or contains(., 'Accept') or contains(., 'Allow')]")
        ]:
            try:
                btn = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((by, selector)))
                driver.execute_script("arguments[0].click();", btn)
                break
            except Exception:
                pass

        try:
            WebDriverWait(driver, 12).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "article.vz-article")))
        except Exception:
            pass

        articles = driver.find_elements(By.CSS_SELECTOR, "article.vz-article")

        for el in articles:
            try:
                link_el = None
                title = ""
                url = ""

                # Pirmiausia bandome tą pačią struktūrą, kuri buvo naudojama ankstesniame VŽ scraperyje.
                try:
                    desc = el.find_element(By.CSS_SELECTOR, "div.vz-article__summary--description")
                    link_el = desc.find_element(By.CSS_SELECTOR, "a")
                    title = (desc.text or "").strip()
                except Exception:
                    try:
                        link_el = el.find_element(By.CSS_SELECTOR, "a[href]")
                        title = (link_el.text or el.text or "").strip()
                    except Exception:
                        link_el = None

                if link_el is None:
                    continue

                url = (link_el.get_attribute("href") or "").strip()
                if not url:
                    continue

                url_key = url.lower()

                # 1) jei jau DB – net neatidarinėjame.
                if url_key in existing_urls:
                    continue

                # 2) jei antraštė neatitinka emitentų – net neatidarinėjame.
                if not _title_matches_issuers(title, issuers):
                    continue

                data_url = extract_date_from_url(url)
                if data_url is None:
                    data_url = date.today()

                full_title, content = _open_new_tab_and_get(driver, url, timeout=18)
                if not full_title:
                    full_title = title

                items.append({
                    "Antraštė": str(full_title or title).strip(),
                    "Nuoroda": url,
                    "Data": data_url,
                    "Pilnas_tekstas": str(content or "").strip(),
                })

            except Exception:
                continue

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return pd.DataFrame(items, columns=["Antraštė", "Nuoroda", "Data", "Pilnas_tekstas"])


# ------------------------------------------------------------
# ATASKAITOS PASIRINKIMAS
# ------------------------------------------------------------
report_param = st.query_params.get("report", "rinkos")
if isinstance(report_param, list):
    report_param = report_param[0] if report_param else "rinkos"

if report_param == "emitentai":
    report_mode = "Emitentų atranka"
elif report_param == "vadovai":
    report_mode = "Vadovų sandoriai"
else:
    report_mode = "Rinkos apžvalga"

with st.sidebar:
    rinkos_active = "active" if report_mode == "Rinkos apžvalga" else ""
    emitentai_active = "active" if report_mode == "Emitentų atranka" else ""
    vadovai_active = "active" if report_mode == "Vadovų sandoriai" else ""
    nav_html = f"""
        <div class="report-nav-title">
            <div class="report-nav-icon">📊</div>
            <div>Ataskaitos</div>
        </div>
        <div class="report-nav">
            <a href="?report=rinkos" target="_self">
                <div class="report-nav-item {rinkos_active}">
                    <div class="nav-icon">📈</div>
                    <div>Rinkos apžvalga</div>
                </div>
            </a>
            <a href="?report=emitentai" target="_self">
                <div class="report-nav-item {emitentai_active}">
                    <div class="nav-icon">👥</div>
                    <div>Emitentų atranka</div>
                </div>
            </a>
            <a href="?report=vadovai" target="_self">
                <div class="report-nav-item {vadovai_active}">
                    <div class="nav-icon">👔</div>
                    <div>Vadovų sandoriai</div>
                </div>
            </a>
        </div>
    """
    st.markdown(nav_html, unsafe_allow_html=True)

    st.markdown(
        """
        <div class="news-db-block">
            <div class="sidebar-section-title">🔄 Naujienų bazė</div>
            <div class="sidebar-section-subtitle">Patikrina naujausius CRIB pranešimus ir atnaujina aktualius VŽ straipsnius pagal DB išsaugotą emitentų sąrašą.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    latest_crib_date = get_latest_crib_news_date()
    if latest_crib_date is not None:
        st.markdown(
            f'<div class="latest-news-date">🕒 Paskutinė DB naujiena:<br><span>{latest_crib_date.strftime("%Y-%m-%d %H:%M")}</span></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="latest-news-date">🕒 Paskutinė DB naujiena:<br><span>nėra duomenų</span></div>',
            unsafe_allow_html=True,
        )

    if st.session_state.news_update_message:
        st.success(st.session_state.news_update_message)
        st.session_state.news_update_message = None

    update_news_btn = st.button(
        "🔄 Atnaujinti duomenis",
        use_container_width=True,
        key="update_crib_news_btn",
    )

    if update_news_btn:
        try:
            crib_inserted = 0
            crib_pages = 0
            vz_inserted = 0
            vz_found = 0
            vz_note = ""

            with st.spinner("Tikrinami nauji CRIB pranešimai..."):
                stats = update_crib_news(
                    max_pages=20,
                    stop_empty_pages=3,
                    headless=True,
                    progress=None,
                )
                crib_inserted = int(stats.get("records_inserted", 0) or 0)
                crib_pages = int(stats.get("pages_processed", 0) or 0)

            # VŽ atnaujinimas naudoja DB išsaugotą emitentų sąrašą.
            # Sąrašas atnaujinamas tik tada, kai sugeneruojama rinkos ataskaita.
            df_issuers_for_vz = None
            with st.spinner("Kraunamas emitentų sąrašas iš DB VŽ atrankai..."):
                try:
                    df_issuers_for_vz = load_issuer_df()
                except Exception as issuer_exc:
                    df_issuers_for_vz = None
                    vz_note = f" VŽ neatnaujinta: nepavyko užkrauti emitentų sąrašo iš DB ({issuer_exc})."

            if (df_issuers_for_vz is None or df_issuers_for_vz.empty) and st.session_state.report_result is not None:
                # Atsarginis variantas: jei DB dar tuščia, panaudojame paskutinę sugeneruotą ataskaitą
                # ir iš karto išsaugome emitentų sąrašą DB ateičiai.
                df_last = st.session_state.report_result.get("df_raw")
                if df_last is not None and not df_last.empty:
                    try:
                        save_issuer_list_from_stat_df(df_last)
                        df_issuers_for_vz = load_issuer_df()
                        vz_note = " Emitentų sąrašas DB buvo tuščias, todėl panaudota paskutinė sugeneruota rinkos ataskaita ir sąrašas išsaugotas DB."
                    except Exception as save_issuer_exc:
                        vz_note = f" VŽ neatnaujinta: nepavyko išsaugoti emitentų sąrašo DB ({save_issuer_exc})."

            if df_issuers_for_vz is not None and not df_issuers_for_vz.empty:
                # VŽ scraperis žiūri tik pirmą VŽ puslapį, todėl datos naudojamos plačiai,
                # kad būtų paimti ir senesni pirmame puslapyje esantys straipsniai.
                vz_start = date(2023, 1, 1)
                vz_end = date.today()
                with st.spinner("Tikrinamas pirmas VŽ puslapis pagal DB emitentų sąrašą..."):
                    vz_df = vz_scrape_first_page_fast(
                        df_issuers_for_vz,
                        progress=None,
                    )
                    vz_found = len(vz_df) if vz_df is not None else 0
                    vz_inserted = save_news_df(vz_df, "vz") if vz_df is not None and not vz_df.empty else 0
            elif not vz_note:
                vz_note = " VŽ neatnaujinta: DB nėra emitentų sąrašo. Sugeneruokite rinkos ataskaitą, kad sąrašas būtų išsaugotas DB."

            st.session_state.report_result = None
            st.session_state.emitentu_result = None
            st.session_state.news_update_message = (
                f"Atnaujinta: CRIB naujai įrašyta {crib_inserted} pranešimų "
                f"(patikrinta puslapių: {crib_pages}); "
                f"VŽ rasta {vz_found}, naujai įrašyta {vz_inserted}."
                f"{vz_note}"
            )
            st.rerun()
        except Exception as exc:
            st.error("Nepavyko atnaujinti naujienų bazės.")
            st.exception(exc)



# ------------------------------------------------------------
# EMITENTŲ ATRANKA: atskira ataskaita, naudojanti tą pačią Supabase market_news lentelę
# ------------------------------------------------------------
if report_mode == "Emitentų atranka":
    with st.sidebar:
        st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
        st.markdown('<div class="sidebar-card-title">🧾 Emitentų atranka</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-card-subtitle">CRIB naujienos imamos iš Supabase DB. Pasirinkite laikotarpį.</div>',
            unsafe_allow_html=True,
        )
        emit_start_date = st.date_input("Nuo", value=date.today(), key="emitentu_start_date")
        emit_end_date = st.date_input("Iki", value=date.today(), key="emitentu_end_date")
        st.markdown('<div class="status-ok">✅ Naudojama market_news lentelė, source=crib</div>', unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

        emit_run_btn = st.button(
            "🚀 Generuoti emitentų atranką",
            type="primary",
            use_container_width=True,
            key="emitentu_run_btn",
        )

    emit_result = st.session_state.emitentu_result

    if emit_result is not None:
        emit_html_bytes = emit_result["html"].encode("utf-8")
        emit_out_name = (
            f"emitentu_atranka_{emit_result['start_date'].strftime('%Y%m%d')}_"
            f"{emit_result['end_date'].strftime('%Y%m%d')}.html"
        )
    else:
        emit_html_bytes = None
        emit_out_name = "emitentu_atranka.html"

    hero_col, download_col = st.columns([5, 1.35])

    with hero_col:
        st.markdown(
            """
            <div class="hero-card">
                <div class="hero-inner">
                    <div class="hero-icon">🧾</div>
                    <div>
                        <h1 class="hero-title">Emitentų atranka</h1>
                        <div class="hero-text">
                            Ataskaita naudoja tas pačias CRIB / Nasdaq emitentų naujienas iš Supabase duomenų bazės,
                            jas klasifikuoja pagal temas ir pateikia HTML peržiūrą su filtrais.
                        </div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with download_col:
        st.markdown('<div class="hero-download">', unsafe_allow_html=True)
        if emit_html_bytes is not None:
            st.download_button(
                label="⬇ Atsisiųsti HTML",
                data=emit_html_bytes,
                file_name=emit_out_name,
                mime="text/html",
                use_container_width=True,
            )
        else:
            st.button("⬇ Atsisiųsti HTML", disabled=True, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    col1.metric("🗓️ Nuo", str(emit_start_date or "-"))
    col2.metric("🗓️ Iki", str(emit_end_date or "-"))

    st.markdown("---")

    if emit_run_btn:
        if emit_start_date is None or emit_end_date is None:
            st.error("Pasirinkite datas.")
            st.stop()

        if emit_start_date > emit_end_date:
            st.error("Data „Nuo“ negali būti vėlesnė už datą „Iki“.")
            st.stop()

        try:
            with st.spinner("Kraunamos CRIB naujienos iš Supabase ir generuojama emitentų atranka..."):
                generated_emit = generate_emitentu_ataskaita(
                    start_date=emit_start_date,
                    end_date=emit_end_date,
                )

            st.session_state.emitentu_result = generated_emit
            st.session_state.emitentu_dates = (emit_start_date, emit_end_date)
            st.rerun()

        except Exception as exc:
            st.exception(exc)
            st.stop()

    emit_result = st.session_state.emitentu_result

    if emit_result is None:
        st.markdown(
            """
            <div class="info-box">
                ℹ️ Pasirinkite laikotarpį ir paspauskite „Generuoti emitentų atranką“.
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.stop()

    st.success(f"Rasta CRIB įrašų: {len(emit_result['df'])}")

    tab1, tab2, tab3 = st.tabs([
        "📊 Kategorijų santrauka",
        "🧾 HTML ataskaita",
        "📄 Duomenys",
    ])

    with tab1:
        if emit_result["summary"].empty:
            st.info("Pasirinktam laikotarpiui klasifikuotinų įrašų nerasta.")
        else:
            st.dataframe(emit_result["summary"], use_container_width=True, hide_index=True)

    with tab2:
        components.html(
            emit_result["html"],
            height=900,
            scrolling=True,
        )

    with tab3:
        df_show = emit_result["df"].copy()
        if "categories" in df_show.columns:
            df_show["categories"] = df_show["categories"].apply(
                lambda x: "; ".join(x) if isinstance(x, (list, tuple)) else str(x)
            )
        st.dataframe(df_show, use_container_width=True, hide_index=True)

    st.stop()

with st.sidebar:
    # ------------------------------------------------------------
    # PAGEIDAUJAMA TVARKA SIDEBAR'E:
    # 1) datos
    # 2) generavimo mygtukas
    # 3) duomenų šaltinis
    # ------------------------------------------------------------
    st.markdown('<div class="sidebar-section-title">🗓️ Laikotarpis</div>', unsafe_allow_html=True)
    start_date = st.date_input("Nuo", value=date.today(), key="rinkos_start_date")
    end_date = st.date_input("Iki", value=date.today(), key="rinkos_end_date")

    run_btn = st.button(
        "🚀 Generuoti ataskaitą",
        type="primary",
        use_container_width=True,
        key="rinkos_run_btn",
    )

    st.markdown(
        """
        <div class="datasource-block">
            <div class="sidebar-section-title">📌 Duomenų šaltinis</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    duomenu_saltinis = st.radio(
        "Pasirinkite duomenų gavimo būdą",
        ["Atsisiųsti iš Nasdaq Baltic", "Įkelti Excel rankiniu būdu"],
        label_visibility="collapsed",
        key="rinkos_duomenu_saltinis",
    )

    uploaded_file = None
    filename = None

    if duomenu_saltinis == "Atsisiųsti iš Nasdaq Baltic":
        st.markdown('<div class="sidebar-section-title">📥 Nasdaq Baltic atsisiuntimas</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-section-subtitle">Bus atsisiųstas Nasdaq statistikos Excel failas pagal aukščiau pasirinktą laikotarpį.</div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="status-ok">✅ Automatinis atsisiuntimas įjungtas</div>', unsafe_allow_html=True)

    else:
        st.markdown('<div class="sidebar-section-title">📄 Statistikos Excel failas</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-section-subtitle">Įkelkite Nasdaq statistikos Excel failą (.xlsx). Ataskaitos laikotarpis bus imamas iš aukščiau pasirinktų datų.</div>',
            unsafe_allow_html=True,
        )

        if st.session_state.uploaded_file_cache is None:
            st.markdown('<div class="upload-area">', unsafe_allow_html=True)

            uploaded_file_temp = st.file_uploader(
                "Statistikos Excel failas",
                type=["xlsx"],
                label_visibility="collapsed",
                key=f"statistics_uploader_{st.session_state.uploader_key}",
            )

            st.markdown("</div>", unsafe_allow_html=True)

            if uploaded_file_temp is not None:
                st.session_state.uploaded_file_cache = uploaded_file_temp
                st.rerun()

        else:
            uploaded_file = st.session_state.uploaded_file_cache

            st.markdown(
                f"""
                <div class="file-status-card">
                    <div class="file-status-row">
                        <div class="file-icon">📄</div>
                        <div class="file-main">
                            <div class="file-name">{uploaded_file.name}</div>
                            <div class="file-meta">{format_size(uploaded_file.size)} · XLSX</div>
                        </div>
                        <div class="file-ok">✓</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            if st.button("🔄 Pakeisti failą", use_container_width=True, key="change_statistics_file_btn"):
                st.session_state.uploader_key += 1
                st.session_state.uploaded_file_cache = None
                st.session_state.report_result = None
                st.session_state.report_filename = None
                st.rerun()

        if uploaded_file is None:
            st.markdown('<div class="status-empty">🛡️ Failas neįkeltas</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="status-ok">✅ Failas įkeltas</div>', unsafe_allow_html=True)


result = st.session_state.report_result

if result is not None:
    html_bytes = result["html"].encode("utf-8")
    out_name = f"rinkos_ataskaita_stilius_{date.today().isoformat()}.html"
else:
    html_bytes = None
    out_name = "rinkos_ataskaita.html"


hero_col, download_col = st.columns([5, 1.35])

with hero_col:
    st.markdown(
        """
        <div class="hero-card">
            <div class="hero-inner">
                <div class="hero-icon">📈</div>
                <div>
                    <h1 class="hero-title">Rinkos pulsas</h1>
                    <div class="hero-text">
                        Įkelkite Nasdaq statistikos Excel failą arba leiskite programai jį atsisiųsti automatiškai.
                        Aplikacija surinks CRIB, VŽ ir Nasdaq naujienas, suformuos lenteles ir HTML ataskaitą.
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with download_col:
    st.markdown('<div class="hero-download">', unsafe_allow_html=True)
    if html_bytes is not None:
        st.download_button(
            label="⬇ Atsisiųsti HTML",
            data=html_bytes,
            file_name=out_name,
            mime="text/html",
            use_container_width=True,
        )
    else:
        st.button(
            "⬇ Atsisiųsti HTML",
            disabled=True,
            use_container_width=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)


st.markdown("<br>", unsafe_allow_html=True)

if duomenu_saltinis == "Atsisiųsti iš Nasdaq Baltic":
    shown_filename = "Bus atsisiųsta iš Nasdaq Baltic"
else:
    shown_filename = uploaded_file.name if uploaded_file is not None else "-"

col1, col2 = st.columns(2)

col1.metric("🗓️ Nuo", str(start_date or "-"))
col2.metric("🗓️ Iki", str(end_date or "-"))

st.markdown("---")


if run_btn:
    if start_date is None or end_date is None:
        st.error("Nepavyko nustatyti datų. Pasirinkite laikotarpį rankiniu būdu.")
        st.stop()

    if start_date > end_date:
        st.error("Data „Nuo“ negali būti vėlesnė už datą „Iki“.")
        st.stop()

    progress_box = st.empty()

    def progress(message: str):
        progress_box.info(message)

    try:
        with st.spinner("Generuojama ataskaita..."):

            if duomenu_saltinis == "Atsisiųsti iš Nasdaq Baltic":
                progress("📥 Atsisiunčiamas Nasdaq Baltic statistikos Excel failas...")

                uploaded_file, filename = download_nasdaq_statistics_excel(
                    start_date=start_date,
                    end_date=end_date,
                    download_dir="downloads",
                    progress=progress,
                )

                progress(f"✅ Failas atsisiųstas: {filename}")

            else:
                if uploaded_file is None:
                    st.error("Įkelkite Excel failą arba pasirinkite automatinį atsisiuntimą iš Nasdaq Baltic.")
                    st.stop()

                filename = uploaded_file.name

            generated = generate_report(
                excel_file=uploaded_file,
                filename=filename,
                start_date=start_date,
                end_date=end_date,
                progress=progress,
            )

        st.session_state.report_result = generated
        st.session_state.report_filename = filename

        try:
            issuer_count = save_issuer_list_from_stat_df(generated.get("df_raw"))
            progress_box.success(f"Ataskaita sugeneruota. Emitentų sąrašas DB atnaujintas: {issuer_count} įrašų.")
        except Exception as issuer_exc:
            progress_box.warning(f"Ataskaita sugeneruota, bet emitentų sąrašo nepavyko išsaugoti DB: {issuer_exc}")

        st.rerun()

    except Exception as exc:
        st.exception(exc)
        st.stop()


result = st.session_state.report_result

if result is None:
    st.markdown(
        """
        <div class="info-box">
            ℹ️ Pasirinkite duomenų šaltinį ir paspauskite „Generuoti ataskaitą“.
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.stop()


tab1, tab2, tab3, tab4, tab5 = st.tabs(
    [
        "📈 Akcijos",
        "🏦 Obligacijos",
        "🌱 First North",
        "📰 Visos naujienos",
        "🧾 Pilna HTML peržiūra",
    ]
)


def show_styled_table(styler):
    st.markdown(
        f'<div class="report-table-wrapper">{styler.to_html()}</div>',
        unsafe_allow_html=True,
    )


with tab1:
    show_styled_table(result["styled_akcijos"])

with tab2:
    show_styled_table(result["styled_obligacijos"])

with tab3:
    show_styled_table(result["styled_first_north"])

with tab4:
    show_styled_table(result["styled_visos"])

with tab5:
    st.components.v1.html(
        result["html"],
        height=900,
        scrolling=True,
    )

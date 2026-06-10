# -*- coding: utf-8 -*-
from datetime import date
import streamlit as st

from rinkos_logika import (
    generate_report,
    extract_dates_from_filename,
    download_nasdaq_statistics_excel,
)


st.set_page_config(
    page_title="Rinkos pulsas",
    page_icon="📊",
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


CSS = """
<style>
.stApp {
    background: #ffffff;
}

.block-container {
    padding-top: 1.2rem;
    padding-left: 2rem;
    padding-right: 2rem;
}

/* SIDEBAR */
section[data-testid="stSidebar"] {
    background: radial-gradient(circle at top left, #0c356b 0%, #061d3a 35%, #03162d 100%) !important;
    min-width: 350px !important;
    max-width: 350px !important;
}

section[data-testid="stSidebar"] > div {
    padding: 28px 18px 24px 18px;
}

section[data-testid="stSidebar"] * {
    color: #ffffff;
}

.sidebar-title {
    font-size: 24px;
    font-weight: 900;
    margin-bottom: 24px;
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


with st.sidebar:
    st.markdown('<div class="sidebar-title">⚙️ Nustatymai</div>', unsafe_allow_html=True)

    st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-card-title">📌 Duomenų šaltinis</div>', unsafe_allow_html=True)

    duomenu_saltinis = st.radio(
        "Pasirinkite duomenų gavimo būdą",
        ["Atsisiųsti iš Nasdaq Baltic", "Įkelti Excel rankiniu būdu"],
        label_visibility="collapsed",
    )

    st.markdown("</div>", unsafe_allow_html=True)

    uploaded_file = None
    filename = None

    if duomenu_saltinis == "Atsisiųsti iš Nasdaq Baltic":
        st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
        st.markdown('<div class="sidebar-card-title">📥 Nasdaq Baltic atsisiuntimas</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-card-subtitle">Pasirinkite laikotarpį. Programa pati atsisiųs Excel failą iš Nasdaq Baltic.</div>',
            unsafe_allow_html=True,
        )

        start_date = st.date_input("Nuo", value=date.today())
        end_date = st.date_input("Iki", value=date.today())

        st.markdown('<div class="status-ok">✅ Automatinis atsisiuntimas įjungtas</div>', unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    else:
        st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
        st.markdown('<div class="sidebar-card-title">📄 Statistikos Excel failas</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-card-subtitle">Įkelkite Nasdaq statistikos Excel failą (.xlsx)</div>',
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

            if st.button("🔄 Pakeisti failą", use_container_width=True):
                st.session_state.uploader_key += 1
                st.session_state.uploaded_file_cache = None
                st.session_state.report_result = None
                st.session_state.report_filename = None
                st.rerun()

        if uploaded_file is None:
            st.markdown('<div class="status-empty">🛡️ Failas neįkeltas</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="status-ok">✅ Failas įkeltas</div>', unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)

        naudoti_rankines_datas = st.checkbox(
            "Datas įvesti rankiniu būdu",
            value=False,
        )

        if uploaded_file is not None and not naudoti_rankines_datas:
            file_start, file_end = extract_dates_from_filename(uploaded_file.name)
            start_date = file_start
            end_date = file_end
        else:
            start_date = st.date_input("Nuo", value=date.today())
            end_date = st.date_input("Iki", value=date.today())

    run_btn = st.button(
        "🚀 Generuoti ataskaitą",
        type="primary",
        use_container_width=True,
    )


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
        progress_box.success("Ataskaita sugeneruota.")
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

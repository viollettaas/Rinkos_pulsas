# -*- coding: utf-8 -*-

from datetime import date
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from rinkos_logika import generate_report, download_nasdaq_statistics_excel
from emitentu_atranka import generate_emitentu_ataskaita
from crib_update import update_crib_news, get_latest_crib_news_date
from issuer_cache import save_issuer_list_from_stat_df, load_issuer_df
from vz_update import update_vz_news_fast

try:
    from manager_transactions_update import update_manager_transactions_from_recent_crib
except Exception:
    update_manager_transactions_from_recent_crib = None

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


# ============================================================
# SESSION STATE
# ============================================================

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


# ============================================================
# CSS
# ============================================================

CSS = """
<style>
.stApp { background: #ffffff; }
.block-container {
    padding-top: 1.2rem;
    padding-left: 2rem;
    padding-right: 2rem;
    max-width: 100% !important;
}
section[data-testid="stSidebar"] {
    background: radial-gradient(circle at top left, #0c356b 0%, #061d3a 35%, #03162d 100%) !important;
    min-width: 350px !important;
    max-width: 350px !important;
}
section[data-testid="stSidebar"] * { color: #ffffff; }

/* Date input laukai sidebar'e: tekstas turi būti matomas vedant laikotarpį */
section[data-testid="stSidebar"] [data-testid="stDateInput"] input {
    color: #061b34 !important;
    background: #ffffff !important;
    caret-color: #061b34 !important;
    -webkit-text-fill-color: #061b34 !important;
}

section[data-testid="stSidebar"] [data-testid="stDateInput"] input::placeholder {
    color: #6b7280 !important;
    -webkit-text-fill-color: #6b7280 !important;
}

section[data-testid="stSidebar"] [data-testid="stDateInput"] svg {
    color: #061b34 !important;
    fill: #061b34 !important;
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
.sidebar-section-title {
    font-size: 16px;
    font-weight: 950;
    margin: 16px 0 8px 0;
}
.sidebar-section-subtitle {
    color: #b8c9df !important;
    font-size: 13px;
    line-height: 1.45;
    margin: 0 0 12px 0;
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
section[data-testid="stSidebar"] .stButton > button,
section[data-testid="stSidebar"] [data-testid="stFileUploader"] button {
    background: linear-gradient(135deg, #1478ff, #0066ff) !important;
    color: white !important;
    border: none !important;
    border-radius: 12px !important;
    height: 48px !important;
    font-weight: 900 !important;
}
.hero-card {
    background: linear-gradient(135deg, #ffffff 0%, #f2f8ff 52%, #dcecff 100%);
    border: 1px solid #dbe7f5;
    border-radius: 18px;
    padding: 28px 32px;
    min-height: 172px;
    box-shadow: 0 8px 28px rgba(8, 44, 84, 0.08);
}
.hero-inner {
    display: flex;
    align-items: flex-start;
    gap: 18px;
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
    max-width: 760px;
}
.hero-download button {
    background: #061b34 !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    min-height: 52px !important;
    font-weight: 900 !important;
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
.report-nav-title {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 25px;
    font-weight: 950;
    margin: 0 0 14px 0;
}
.report-nav-icon {
    width: 38px;
    height: 38px;
    border-radius: 11px;
    background: rgba(255,255,255,0.16);
    display: flex;
    align-items: center;
    justify-content: center;
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
}
.report-nav-item.active {
    background: rgba(20,120,255,0.14);
    border: 3px solid #68bdff;
}
.report-nav-item .nav-icon {
    font-size: 24px;
    width: 28px;
    text-align: center;
}
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
.report-table-wrapper th {
    background: #061b34 !important;
    color: white !important;
    padding: 12px 10px !important;
    font-size: 12px !important;
    text-align: left !important;
}
.report-table-wrapper td {
    padding: 10px !important;
    font-size: 12px !important;
    color: #102033 !important;
    border-bottom: 1px solid #e7eef7 !important;
}
</style>
"""

st.markdown(CSS, unsafe_allow_html=True)


# ============================================================
# PAGALBINĖS FUNKCIJOS
# ============================================================

def format_size(size_bytes: int) -> str:
    if size_bytes is None:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def prepare_emitentu_table_df(emit_df: pd.DataFrame) -> pd.DataFrame:
    if emit_df is None or emit_df.empty:
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

    df = emit_df.copy()

    if "date" in df.columns:
        dt = pd.to_datetime(df["date"], errors="coerce")
        df["data"] = dt.dt.strftime("%Y-%m-%d %H:%M")
        df["data"] = df["data"].fillna(df["date"].astype(str))
    else:
        df["data"] = ""

    if "categories" in df.columns:
        df["kategorijos"] = df["categories"].apply(
            lambda x: ", ".join(x) if isinstance(x, (list, tuple)) else str(x)
        )
    elif "categories_str" in df.columns:
        df["kategorijos"] = df["categories_str"].fillna("").astype(str)
    else:
        df["kategorijos"] = ""

    rename_map = {
        "issuer": "emitentas",
        "type": "tipas",
        "title": "antraste",
        "summary": "santrauka",
        "matched_keywords": "raktazodziai",
        "url": "nuoroda",
    }

    for old, new in rename_map.items():
        if old in df.columns:
            df[new] = df[old].fillna("").astype(str)
        elif new not in df.columns:
            df[new] = ""

    return df[[
        "data",
        "emitentas",
        "kategorijos",
        "tipas",
        "antraste",
        "santrauka",
        "raktazodziai",
        "nuoroda",
    ]]


def filter_emitentu_table_df(
    df_view: pd.DataFrame,
    search: str = "",
    selected_issuers=None,
    selected_categories=None,
) -> pd.DataFrame:
    if df_view is None or df_view.empty:
        return df_view

    out = df_view.copy()

    if selected_issuers:
        out = out[out["emitentas"].isin(selected_issuers)]

    if selected_categories:
        out = out[
            out["kategorijos"].astype(str).apply(
                lambda x: any(cat in x for cat in selected_categories)
            )
        ]

    if search and search.strip():
        q = search.strip().lower()

        mask = pd.Series(False, index=out.index)

        for col in [
            "data",
            "emitentas",
            "kategorijos",
            "tipas",
            "antraste",
            "santrauka",
            "raktazodziai",
        ]:
            mask = mask | out[col].astype(str).str.lower().str.contains(
                q,
                na=False,
                regex=False,
            )

        out = out[mask]

    return out


def show_styled_table(styler):
    st.markdown(
        f'<div class="report-table-wrapper">{styler.to_html()}</div>',
        unsafe_allow_html=True,
    )


# ============================================================
# ATASKAITOS PASIRINKIMAS
# ============================================================

report_param = st.query_params.get("report", "rinkos")

if isinstance(report_param, list):
    report_param = report_param[0] if report_param else "rinkos"

if report_param == "emitentai":
    report_mode = "Emitentų atranka"
elif report_param == "vadovai":
    report_mode = "Vadovų sandoriai"
else:
    report_mode = "Rinkos apžvalga"


# ============================================================
# SIDEBAR NAVIGACIJA IR DB ATNAUJINIMAS
# ============================================================

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
        <div class="sidebar-section-title">🔄 Naujienų bazė</div>
        <div class="sidebar-section-subtitle">
        Patikrina naujausius CRIB pranešimus ir atnaujina aktualius VŽ straipsnius.
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

            manager_note = ""

            if update_manager_transactions_from_recent_crib is not None:
                with st.spinner("Tikrinami vadovų sandorių CRIB pranešimai..."):
                    mgr_stats = update_manager_transactions_from_recent_crib(
                        days_back=45,
                        max_messages=30,
                        headless=True,
                        progress=None,
                    )

                manager_found = int(mgr_stats.get("manager_messages_found", 0) or 0)
                manager_processed = int(mgr_stats.get("manager_messages_processed", 0) or 0)
                manager_saved = int(mgr_stats.get("manager_transactions_saved", 0) or 0)

                manager_note = (
                    f" Vadovų sandoriai: rasta CRIB pranešimų {manager_found}, "
                    f"apdorota {manager_processed}, įrašyta {manager_saved};"
                )
            else:
                manager_note = " Vadovų sandoriai neatnaujinti: modulis nerastas;"

            df_issuers_for_vz = None

            with st.spinner("Kraunamas emitentų sąrašas VŽ atrankai..."):
                try:
                    df_issuers_for_vz = load_issuer_df()
                except Exception as issuer_exc:
                    vz_note = f" VŽ neatnaujinta: nepavyko užkrauti emitentų sąrašo ({issuer_exc})."

            if df_issuers_for_vz is not None and not df_issuers_for_vz.empty:
                with st.spinner("Tikrinamas VŽ puslapis pagal emitentų sąrašą..."):
                    vz_stats = update_vz_news_fast(
                        df_issuers=df_issuers_for_vz,
                        existing_url_limit=800,
                        max_articles=80,
                        progress=None,
                    )

                vz_found = int(vz_stats.get("found", 0) or 0)
                vz_inserted = int(vz_stats.get("inserted", 0) or 0)
                vz_checked = int(vz_stats.get("checked", 0) or 0)
                vz_matched = int(vz_stats.get("matched", 0) or 0)

                vz_note += (
                    f" VŽ patikrinta {vz_checked} straipsnių, "
                    f"aktualių kandidatų {vz_matched}."
                )
            elif not vz_note:
                vz_note = " VŽ neatnaujinta: DB nėra emitentų sąrašo."

            st.session_state.report_result = None
            st.session_state.emitentu_result = None

            st.session_state.news_update_message = (
                f"Atnaujinta: CRIB naujai įrašyta {crib_inserted} pranešimų "
                f"(patikrinta puslapių: {crib_pages});"
                f"{manager_note} "
                f"VŽ rasta {vz_found}, naujai įrašyta {vz_inserted}."
                f"{vz_note}"
            )

            st.rerun()

        except Exception as exc:
            st.error("Nepavyko atnaujinti naujienų bazės.")
            st.exception(exc)


# ============================================================
# VADOVŲ SANDORIAI
# ============================================================

if report_mode == "Vadovų sandoriai":
    if show_manager_transactions_page is None:
        st.error("Nepavyko užkrauti vadovų sandorių modulio vadovu_sandoriai.py.")
        st.stop()

    show_manager_transactions_page()
    st.stop()


# ============================================================
# EMITENTŲ ATRANKA
# ============================================================

if report_mode == "Emitentų atranka":
    with st.sidebar:
        st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
        st.markdown('<div class="sidebar-card-title">🧾 Emitentų atranka</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-card-subtitle">CRIB naujienos imamos iš Supabase DB. Pasirinkite laikotarpį.</div>',
            unsafe_allow_html=True,
        )

        emit_start_date = st.date_input(
            "Nuo",
            value=date.today(),
            key="emitentu_start_date",
        )

        emit_end_date = st.date_input(
            "Iki",
            value=date.today(),
            key="emitentu_end_date",
        )

        st.markdown(
            '<div class="status-ok">✅ Naudojama market_news lentelė, source=crib</div>',
            unsafe_allow_html=True,
        )
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
                            CRIB pranešimų peržiūra su paieška, emitentų ir kategorijų filtrais.
                            Kategorijų santraukos šiame vaizde nebėra.
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
        "📋 Interaktyvi lentelė",
        "🧾 HTML ataskaita",
        "⬇️ Atsisiuntimas",
    ])

    with tab1:
        df_view = prepare_emitentu_table_df(emit_result["df"])

        if df_view.empty:
            st.info("Pasirinktam laikotarpiui įrašų nerasta.")
        else:
            st.markdown("#### Paieška ir filtrai")

            search = st.text_input(
                "Paieškos žodis",
                placeholder="Pvz. teism, dividendai, vadovas, nuostoliai, obligacijos...",
                key="emitentu_search",
            )

            fcol1, fcol2 = st.columns(2)

            with fcol1:
                issuers = sorted(df_view["emitentas"].dropna().unique().tolist())

                selected_issuers = st.multiselect(
                    "Emitentai",
                    options=issuers,
                    default=[],
                    key="emitentu_issuer_filter",
                )

            with fcol2:
                all_categories = sorted({
                    cat.strip()
                    for value in df_view["kategorijos"].dropna().astype(str)
                    for cat in value.replace(";", ",").split(",")
                    if cat.strip()
                })

                selected_categories = st.multiselect(
                    "Kategorijos",
                    options=all_categories,
                    default=[],
                    key="emitentu_category_filter",
                )

            filtered = filter_emitentu_table_df(
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

    with tab2:
        components.html(
            emit_result["html"],
            height=900,
            scrolling=True,
        )

    with tab3:
        df_export = prepare_emitentu_table_df(emit_result["df"])
        csv_data = df_export.to_csv(index=False).encode("utf-8-sig")

        st.download_button(
            "⬇ Atsisiųsti lentelę CSV formatu",
            data=csv_data,
            file_name=(
                f"emitentu_atranka_{emit_result['start_date'].strftime('%Y%m%d')}_"
                f"{emit_result['end_date'].strftime('%Y%m%d')}.csv"
            ),
            mime="text/csv",
            use_container_width=True,
        )

        st.download_button(
            "⬇ Atsisiųsti HTML ataskaitą",
            data=emit_result["html"].encode("utf-8"),
            file_name=(
                f"emitentu_atranka_{emit_result['start_date'].strftime('%Y%m%d')}_"
                f"{emit_result['end_date'].strftime('%Y%m%d')}.html"
            ),
            mime="text/html",
            use_container_width=True,
        )

    st.stop()


# ============================================================
# RINKOS APŽVALGA
# ============================================================

with st.sidebar:
    st.markdown('<div class="sidebar-section-title">🗓️ Laikotarpis</div>', unsafe_allow_html=True)

    start_date = st.date_input(
        "Nuo",
        value=date.today(),
        key="rinkos_start_date",
    )

    end_date = st.date_input(
        "Iki",
        value=date.today(),
        key="rinkos_end_date",
    )

    run_btn = st.button(
        "🚀 Generuoti ataskaitą",
        type="primary",
        use_container_width=True,
        key="rinkos_run_btn",
    )

    st.markdown(
        """
        <div class="sidebar-section-title">📌 Duomenų šaltinis</div>
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
        st.markdown(
            '<div class="status-ok">✅ Automatinis atsisiuntimas įjungtas</div>',
            unsafe_allow_html=True,
        )

    else:
        st.markdown(
            '<div class="sidebar-section-subtitle">Įkelkite Nasdaq statistikos Excel failą (.xlsx).</div>',
            unsafe_allow_html=True,
        )

        if st.session_state.uploaded_file_cache is None:
            uploaded_file_temp = st.file_uploader(
                "Statistikos Excel failas",
                type=["xlsx"],
                label_visibility="collapsed",
                key=f"statistics_uploader_{st.session_state.uploader_key}",
            )

            if uploaded_file_temp is not None:
                st.session_state.uploaded_file_cache = uploaded_file_temp
                st.rerun()

        else:
            uploaded_file = st.session_state.uploaded_file_cache
            st.markdown(
                f'<div class="status-ok">✅ Failas įkeltas: {uploaded_file.name}</div>',
                unsafe_allow_html=True,
            )

            if st.button(
                "🔄 Pakeisti failą",
                use_container_width=True,
                key="change_statistics_file_btn",
            ):
                st.session_state.uploader_key += 1
                st.session_state.uploaded_file_cache = None
                st.session_state.report_result = None
                st.session_state.report_filename = None
                st.rerun()

        if uploaded_file is None:
            st.markdown(
                '<div class="status-empty">🛡️ Failas neįkeltas</div>',
                unsafe_allow_html=True,
            )


result = st.session_state.report_result

if result is not None:
    html_bytes = result["html"].encode("utf-8")
    out_name = f"rinkos_ataskaita_{date.today().isoformat()}.html"
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
            progress_box.success(
                f"Ataskaita sugeneruota. Emitentų sąrašas DB atnaujintas: {issuer_count} įrašų."
            )
        except Exception as issuer_exc:
            progress_box.warning(
                f"Ataskaita sugeneruota, bet emitentų sąrašo nepavyko išsaugoti DB: {issuer_exc}"
            )

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


tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📈 Akcijos",
    "🏦 Obligacijos",
    "🌱 First North",
    "📰 Visos naujienos",
    "🧾 Pilna HTML peržiūra",
])


with tab1:
    show_styled_table(result["styled_akcijos"])

with tab2:
    show_styled_table(result["styled_obligacijos"])

with tab3:
    show_styled_table(result["styled_first_north"])

with tab4:
    show_styled_table(result["styled_visos"])

with tab5:
    components.html(
        result["html"],
        height=900,
        scrolling=True,
    )

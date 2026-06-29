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

try:
    from metines import show_annual_reports_page
except Exception:
    show_annual_reports_page = None


st.set_page_config(
    page_title="Rinkos pulsas",
    page_icon="",
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
.nav-card {
    border: 1px solid rgba(120, 120, 120, 0.25);
    border-radius: 14px;
    padding: 14px 14px 10px 14px;
    margin-bottom: 16px;
}
.nav-title {
    font-size: 0.9rem;
    font-weight: 700;
    margin-bottom: 10px;
    opacity: 0.85;
}
.nav-link {
    display: block;
    padding: 8px 10px;
    margin: 4px 0;
    border-radius: 10px;
    text-decoration: none !important;
    color: inherit !important;
    border: 1px solid transparent;
}
.nav-link:hover {
    border: 1px solid rgba(120, 120, 120, 0.35);
}
.nav-link.active {
    background: rgba(90, 140, 255, 0.14);
    border: 1px solid rgba(90, 140, 255, 0.35);
    font-weight: 700;
}
.sidebar-card {
    border: 1px solid rgba(120, 120, 120, 0.25);
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 16px;
}
.sidebar-card-title {
    font-weight: 700;
    margin-bottom: 8px;
}
.sidebar-card-subtitle {
    font-size: 0.86rem;
    opacity: 0.75;
    margin-bottom: 10px;
}
.status-muted {
    font-size: 0.86rem;
    opacity: 0.8;
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
        return pd.DataFrame(
            columns=[
                "data",
                "emitentas",
                "kategorijos",
                "tipas",
                "antraste",
                "santrauka",
                "raktazodziai",
                "nuoroda",
            ]
        )

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

    return df[
        [
            "data",
            "emitentas",
            "kategorijos",
            "tipas",
            "antraste",
            "santrauka",
            "raktazodziai",
            "nuoroda",
        ]
    ]


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
        f'<div class="styled-table-wrap">{styler.to_html()}</div>',
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
elif report_param == "metines":
    report_mode = "Metinės ataskaitos"
else:
    report_mode = "Rinkos apžvalga"

# ============================================================
# SIDEBAR NAVIGACIJA IR DB ATNAUJINIMAS
# ============================================================

with st.sidebar:
    rinkos_active = "active" if report_mode == "Rinkos apžvalga" else ""
    emitentai_active = "active" if report_mode == "Emitentų atranka" else ""
    vadovai_active = "active" if report_mode == "Vadovų sandoriai" else ""
    metines_active = "active" if report_mode == "Metinės ataskaitos" else ""

    nav_html = f"""
    <div class="nav-card">
        <div class="nav-title">Ataskaitos</div>
        <a class="nav-link {rinkos_active}" href="?report=rinkos" target="_self">Rinkos apžvalga</a>
        <a class="nav-link {emitentai_active}" href="?report=emitentai" target="_self">Emitentų atranka</a>
        <a class="nav-link {vadovai_active}" href="?report=vadovai" target="_self">Vadovų sandoriai</a>
        <a class="nav-link {metines_active}" href="?report=metines" target="_self">Metinės ataskaitos</a>
    </div>
    """
    st.markdown(nav_html, unsafe_allow_html=True)

    st.markdown(
        """
        <div class="sidebar-card">
            <div class="sidebar-card-title">Naujienų bazė</div>
            <div class="sidebar-card-subtitle">
                Patikrina naujausius CRIB pranešimus ir atnaujina aktualius VŽ straipsnius.
            </div>
        """,
        unsafe_allow_html=True,
    )

    latest_crib_date = get_latest_crib_news_date()
    if latest_crib_date is not None:
        st.markdown(
            f'<div class="status-muted">Paskutinė DB naujiena:<br><b>{latest_crib_date.strftime("%Y-%m-%d %H:%M")}</b></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="status-muted">Paskutinė DB naujiena:<br><b>nėra duomenų</b></div>',
            unsafe_allow_html=True,
        )

    if st.session_state.news_update_message:
        st.success(st.session_state.news_update_message)
        st.session_state.news_update_message = None

    update_news_btn = st.button(
        "Atnaujinti duomenis",
        use_container_width=True,
        key="update_crib_news_btn",
    )
    st.markdown("</div>", unsafe_allow_html=True)

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
# METINĖS ATASKAITOS
# ============================================================

if report_mode == "Metinės ataskaitos":
    if show_annual_reports_page is None:
        st.error("Nepavyko užkrauti metinių ataskaitų modulio metines.py.")
        st.stop()
    show_annual_reports_page()
    st.stop()

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
        st.markdown('<div class="sidebar-card-title">Emitentų atranka</div>', unsafe_allow_html=True)
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
            '<div class="status-muted">Naudojama market_news lentelė, source=crib</div>',
            unsafe_allow_html=True,
        )
        emit_run_btn = st.button(
            "Generuoti emitentų atranką",
            type="primary",
            use_container_width=True,
            key="emitentu_run_btn",
        )
        st.markdown("</div>", unsafe_allow_html=True)

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
            # Emitentų atranka

            CRIB pranešimų peržiūra su paieška, emitentų ir kategorijų filtrais.
            Kategorijų santraukos šiame vaizde nebėra.
            """,
            unsafe_allow_html=True,
        )
    with download_col:
        st.markdown("<br>", unsafe_allow_html=True)
        if emit_html_bytes is not None:
            st.download_button(
                label="Atsisiųsti HTML",
                data=emit_html_bytes,
                file_name=emit_out_name,
                mime="text/html",
                use_container_width=True,
            )
        else:
            st.button("Atsisiųsti HTML", disabled=True, use_container_width=True)

    st.markdown("<br>", unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    col1.metric("Nuo", str(emit_start_date or "-"))
    col2.metric("Iki", str(emit_end_date or "-"))
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
        st.markdown("ℹ️ Pasirinkite laikotarpį ir paspauskite „Generuoti emitentų atranką“.")
        st.stop()

    st.success(f"Rasta CRIB įrašų: {len(emit_result['df'])}")
    tab1, tab2 = st.tabs(["HTML ataskaita", "Atsisiuntimas"])

    with tab1:
        components.html(
            emit_result["html"],
            height=1250,
            scrolling=True,
        )

    with tab2:
        df_export = prepare_emitentu_table_df(emit_result["df"])
        csv_data = df_export.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "Atsisiųsti lentelę CSV formatu",
            data=csv_data,
            file_name=(
                f"emitentu_atranka_{emit_result['start_date'].strftime('%Y%m%d')}_"
                f"{emit_result['end_date'].strftime('%Y%m%d')}.csv"
            ),
            mime="text/csv",
            use_container_width=True,
        )
        st.download_button(
            "Atsisiųsti HTML ataskaitą",
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
    st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-card-title">Laikotarpis</div>', unsafe_allow_html=True)
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
        "Generuoti ataskaitą",
        type="primary",
        use_container_width=True,
        key="rinkos_run_btn",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-card-title">Duomenų šaltinis</div>', unsafe_allow_html=True)
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
            '<div class="status-muted">Automatinis atsisiuntimas įjungtas</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="sidebar-card-subtitle">Įkelkite Nasdaq statistikos Excel failą (.xlsx).</div>',
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
                f'<div class="status-muted">Failas įkeltas: <b>{uploaded_file.name}</b></div>',
                unsafe_allow_html=True,
            )
            if st.button(
                "Pakeisti failą",
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
                '<div class="status-muted">Failas neįkeltas</div>',
                unsafe_allow_html=True,
            )
    st.markdown("</div>", unsafe_allow_html=True)

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
        # Rinkos pulsas

        Įkelkite Nasdaq statistikos Excel failą arba leiskite programai jį atsisiųsti automatiškai.
        Aplikacija surinks CRIB, VŽ ir Nasdaq naujienas, suformuos lenteles ir HTML ataskaitą.
        """,
        unsafe_allow_html=True,
    )
with download_col:
    st.markdown("<br>", unsafe_allow_html=True)
    if html_bytes is not None:
        st.download_button(
            label="Atsisiųsti HTML",
            data=html_bytes,
            file_name=out_name,
            mime="text/html",
            use_container_width=True,
        )
    else:
        st.button(
            "Atsisiųsti HTML",
            disabled=True,
            use_container_width=True,
        )

st.markdown("<br>", unsafe_allow_html=True)
col1, col2 = st.columns(2)
col1.metric("Nuo", str(start_date or "-"))
col2.metric("Iki", str(end_date or "-"))
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
                progress("Atsisiunčiamas Nasdaq Baltic statistikos Excel failas...")
                uploaded_file, filename = download_nasdaq_statistics_excel(
                    start_date=start_date,
                    end_date=end_date,
                    download_dir="downloads",
                    progress=progress,
                )
                progress(f"Failas atsisiųstas: {filename}")
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
    st.markdown("ℹ️ Pasirinkite duomenų šaltinį ir paspauskite „Generuoti ataskaitą“.")
    st.stop()

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    [
        "Akcijos",
        "Obligacijos",
        "First North",
        "Visos naujienos",
        "Pilna HTML peržiūra",
    ]
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
    components.html(
        result["html"],
        height=900,
        scrolling=True,
    )


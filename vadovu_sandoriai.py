# -*- coding: utf-8 -*-

import re
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from supabase_cache import load_manager_transactions_df, load_crib_news_df


# ============================================================
# DPL / UŽDAROJO LAIKOTARPIO LOGIKA
# ============================================================

ANNUAL_PATTERNS = [
    r"\bmetin(?:ė|e|is|io|ių|ės)\b",
    r"\bmetin(?:is|ė)\s+pranešim",
    r"\bmetin(?:ės|ė|iai|iu?)\s+finansin",
    r"\baudituot",
    r"\baudited\b",
    r"\bannual\s+report\b",
    r"\byear[- ]end\b",
]

HALF_YEAR_PATTERNS = [
    r"\b6\s*m[ėe]n",
    r"\bšešių\s+m[ėe]nesių\b",
    r"\bpusme(?:čio|tis|tį|čiui)\b",
    r"\bhalf[- ]year\b",
    r"\bhalf[- ]yearly\b",
    r"\bsemi[- ]annual\b",
    r"\binterim\s+report\b",
    r"\b6\s*months\b",
    r"\bsix\s+months\b",
]

EXCLUDE_PATTERNS = [
    r"\b3\s*m[ėe]n",
    r"\b9\s*m[ėe]n",
    r"\bq1\b",
    r"\bq3\b",
    r"\bI\s+ketv",
    r"\bIII\s+ketv",
    r"\bketvirčio\b",
    r"\bpreliminar",
    r"\bprognoz",
    r"\bdividend",
    r"\bšaukia\b",
    r"\bsušauk",
]


def _norm_text(x) -> str:
    return str(x or "").lower().strip()


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def _classify_financial_report(row) -> str:
    category = _norm_text(row.get("category", ""))
    title = _norm_text(row.get("title", ""))

    text = f"{category} {title}"

    if _matches_any(text, EXCLUDE_PATTERNS):
        return ""

    is_annual_category = "metin" in category
    is_interim_category = "tarpin" in category

    if is_annual_category and _matches_any(text, ANNUAL_PATTERNS):
        return "Metinė"

    if is_interim_category and _matches_any(text, HALF_YEAR_PATTERNS):
        return "Pusmečio / 6 mėn."

    return ""


def prepare_dpl_periods_df(news_df: pd.DataFrame) -> pd.DataFrame:
    """
    Tikisi CRIB naujienų dataframe su stulpeliais:
    issuer, category, title, published_at arba published_date, crib_url.
    """

    if news_df is None or news_df.empty:
        return pd.DataFrame()

    df = news_df.copy()

    for col in ["issuer", "category", "title", "crib_url"]:
        if col not in df.columns:
            df[col] = ""

    if "published_at" not in df.columns and "published_date" not in df.columns:
        return pd.DataFrame()

    date_col = "published_at" if "published_at" in df.columns else "published_date"

    df["report_published_date"] = pd.to_datetime(
        df[date_col],
        errors="coerce",
        utc=True,
    ).dt.date

    df["issuer"] = df["issuer"].fillna("").astype(str).str.strip()
    df["category"] = df["category"].fillna("").astype(str).str.strip()
    df["title"] = df["title"].fillna("").astype(str).str.strip()

    df["dpl_report_type"] = df.apply(_classify_financial_report, axis=1)

    df = df[
        (df["issuer"] != "")
        & df["report_published_date"].notna()
        & (df["dpl_report_type"] != "")
    ].copy()

    if df.empty:
        return pd.DataFrame()

    df["dpl_start_date"] = df["report_published_date"].apply(
        lambda x: x - timedelta(days=30)
    )
    df["dpl_end_date"] = df["report_published_date"]

    return df[
        [
            "issuer",
            "dpl_report_type",
            "report_published_date",
            "dpl_start_date",
            "dpl_end_date",
            "title",
            "category",
            "crib_url",
        ]
    ].drop_duplicates()


def add_dpl_check_to_transactions(
    transactions_df: pd.DataFrame,
    dpl_periods_df: pd.DataFrame,
) -> pd.DataFrame:
    if transactions_df is None or transactions_df.empty:
        return transactions_df

    df = transactions_df.copy()

    df["is_dpl_period"] = False
    df["dpl_report_type"] = ""
    df["dpl_report_date"] = pd.NaT
    df["dpl_days_to_report"] = pd.NA
    df["dpl_report_title"] = ""
    df["dpl_report_url"] = ""

    if dpl_periods_df is None or dpl_periods_df.empty:
        return df

    periods = dpl_periods_df.copy()

    periods["issuer_key"] = (
        periods["issuer"].fillna("").astype(str).str.lower().str.strip()
    )
    df["issuer_key"] = (
        df["issuer"].fillna("").astype(str).str.lower().str.strip()
    )

    for idx, row in df.iterrows():
        issuer = row.get("issuer_key", "")
        trade_date = row.get("transaction_date_dt")

        if not issuer or pd.isna(trade_date):
            continue

        matches = periods[
            (periods["issuer_key"] == issuer)
            & (periods["dpl_start_date"] <= trade_date)
            & (periods["dpl_end_date"] >= trade_date)
        ].copy()

        if matches.empty:
            continue

        matches["days_to_report"] = matches["report_published_date"].apply(
            lambda x: (x - trade_date).days
        )

        match = matches.sort_values("days_to_report").iloc[0]

        df.at[idx, "is_dpl_period"] = True
        df.at[idx, "dpl_report_type"] = match["dpl_report_type"]
        df.at[idx, "dpl_report_date"] = match["report_published_date"]
        df.at[idx, "dpl_days_to_report"] = match["days_to_report"]
        df.at[idx, "dpl_report_title"] = match["title"]
        df.at[idx, "dpl_report_url"] = match["crib_url"]

    df.drop(columns=["issuer_key"], inplace=True, errors="ignore")

    return df


# ============================================================
# VADOVŲ SANDORIŲ PARUOŠIMAS
# ============================================================

def _to_date_series(series):
    return pd.to_datetime(series, errors="coerce", utc=True).dt.date


def prepare_manager_transactions_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    for col in ["published_at", "transaction_date"]:
        if col not in df.columns:
            df[col] = None

    published_dt = pd.to_datetime(df["published_at"], errors="coerce", utc=True)
    transaction_dt = pd.to_datetime(df["transaction_date"], errors="coerce")

    df["published_date"] = published_dt.dt.date
    df["transaction_date_dt"] = transaction_dt.dt.date

    df["days_to_publish"] = (
        pd.to_datetime(df["published_date"], errors="coerce")
        - pd.to_datetime(df["transaction_date_dt"], errors="coerce")
    ).dt.days

    df["is_late_notification"] = df["days_to_publish"].apply(
        lambda x: bool(pd.notna(x) and x > 3)
    )

    for col in [
        "issuer",
        "person_name",
        "person_role",
        "isin",
        "instrument",
        "transaction_type",
        "venue",
        "parse_status",
        "price_quantity_note",
    ]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str).str.strip()

    for col in ["price", "quantity"]:
        if col not in df.columns:
            df[col] = None
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["transaction_value"] = df["price"] * df["quantity"]

    return df


# ============================================================
# STREAMLIT UI
# ============================================================

def _apply_multiselect_filter(df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    if col not in df.columns:
        return df

    values = sorted([x for x in df[col].dropna().unique() if str(x).strip()])
    selected = st.multiselect(label, values, key=f"mgr_filter_{col}")

    if selected:
        return df[df[col].isin(selected)]

    return df


def _show_summary_cards(df: pd.DataFrame):
    total = len(df)
    issuers = (
        df["issuer"].replace("", pd.NA).dropna().nunique()
        if "issuer" in df.columns
        else 0
    )
    persons = (
        df["person_name"].replace("", pd.NA).dropna().nunique()
        if "person_name" in df.columns
        else 0
    )
    late = (
        int(df["is_late_notification"].sum())
        if "is_late_notification" in df.columns
        else 0
    )
    dpl = (
        int(df["is_dpl_period"].sum())
        if "is_dpl_period" in df.columns
        else 0
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Pranešimų / PDF", total)
    c2.metric("Emitentų", issuers)
    c3.metric("Asmenų", persons)
    c4.metric("Vėluojančių >3 d.", late)
    c5.metric("Per DPL", dpl)


def _show_tables(df: pd.DataFrame):
    st.subheader("1. Detali vadovų sandorių lentelė")

    detail_cols = [
        "published_date",
        "transaction_date_dt",
        "days_to_publish",
        "is_late_notification",

        "is_dpl_period",
        "dpl_report_type",
        "dpl_report_date",
        "dpl_days_to_report",
        "dpl_report_title",
        "dpl_report_url",

        "issuer",
        "person_name",
        "person_role",
        "isin",
        "instrument",
        "transaction_type",
        "price",
        "quantity",
        "transaction_value",
        "price_quantity_note",
        "venue",
        "parse_status",
        "pdf_url",
        "crib_url",
    ]

    detail_cols = [c for c in detail_cols if c in df.columns]

    st.dataframe(
        df[detail_cols],
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("2. Santrauka pagal asmenį")

    agg_dict = {
        "pranesimu_sk": ("pdf_url", "count"),
        "sandorio_bendra_verte": ("transaction_value", "sum"),
        "veluojanciu_sk": ("is_late_notification", "sum"),
        "vid_dienu_iki_pranesimo": ("days_to_publish", "mean"),
    }

    if "is_dpl_period" in df.columns:
        agg_dict["dpl_sandoriu_sk"] = ("is_dpl_period", "sum")

    person_summary = (
        df.groupby(["issuer", "person_name"], dropna=False)
        .agg(**agg_dict)
        .reset_index()
        .sort_values(
            ["pranesimu_sk", "sandorio_bendra_verte"],
            ascending=[False, False],
        )
    )

    st.dataframe(
        person_summary,
        use_container_width=True,
        hide_index=True,
    )


def show_manager_transactions_page():
    st.markdown(
        """
        <div class="hero-card">
            <div class="hero-inner">
                <div class="hero-icon">👔</div>
                <div>
                    <h1 class="hero-title">Vadovų sandoriai</h1>
                    <div class="hero-text">
                        CRIB kategorijos „Pranešimai apie vadovų sandorius“ PDF dokumentai.
                        Lentelėje papildomai skaičiuojamas dienų skaičius nuo sandorio datos
                        iki pranešimo paskelbimo ir tikrinama, ar sandoris vyko DPL laikotarpiu.
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)

    with st.sidebar:
        st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
        st.markdown(
            '<div class="sidebar-card-title">👔 Vadovų sandoriai</div>',
            unsafe_allow_html=True,
        )

        manager_start_date = st.date_input(
            "Pranešimo data nuo",
            value=date.today() - timedelta(days=30),
            key="manager_start_date",
        )

        manager_end_date = st.date_input(
            "Pranešimo data iki",
            value=date.today(),
            key="manager_end_date",
        )

        st.markdown("</div>", unsafe_allow_html=True)

    if manager_start_date > manager_end_date:
        st.error("Data „nuo“ negali būti vėlesnė už datą „iki“.")
        st.stop()

    raw_df = load_manager_transactions_df(
        manager_start_date,
        manager_end_date,
    )

    df = prepare_manager_transactions_df(raw_df)

    if df.empty:
        st.info("Pasirinktu laikotarpiu vadovų sandorių duomenų nėra.")
        st.stop()

    # Ieškome CRIB metinių ir pusmečio ataskaitų plačiau,
    # nes sandoris gali būti prieš vėliau paskelbtą ataskaitą.
    news_start_date = manager_start_date - timedelta(days=370)
    news_end_date = manager_end_date + timedelta(days=370)

    crib_news_df = load_crib_news_df(
        news_start_date,
        news_end_date,
    )

    dpl_periods_df = prepare_dpl_periods_df(crib_news_df)

    df = add_dpl_check_to_transactions(
        df,
        dpl_periods_df,
    )

    _show_summary_cards(df)

    st.markdown("---")

    with st.expander("Filtrai", expanded=True):
        c1, c2, c3, c4 = st.columns(4)

        with c1:
            df = _apply_multiselect_filter(df, "issuer", "Emitentas")

        with c2:
            df = _apply_multiselect_filter(
                df,
                "person_name",
                "Vadovas / susijęs asmuo",
            )

        with c3:
            df = _apply_multiselect_filter(
                df,
                "transaction_type",
                "Sandorio pobūdis",
            )

        with c4:
            df = _apply_multiselect_filter(
                df,
                "parse_status",
                "Apdorojimo statusas",
            )

        delay_filter = st.selectbox(
            "Vėlavimo filtras",
            [
                "Visi",
                "Tik vėluojantys >3 d.",
                "Tik nevėluojantys <=3 d.",
                "Be apskaičiuoto termino",
            ],
            key="mgr_delay_filter",
        )

        if delay_filter == "Tik vėluojantys >3 d.":
            df = df[df["is_late_notification"] == True]
        elif delay_filter == "Tik nevėluojantys <=3 d.":
            df = df[
                (df["days_to_publish"].notna())
                & (df["days_to_publish"] <= 3)
            ]
        elif delay_filter == "Be apskaičiuoto termino":
            df = df[df["days_to_publish"].isna()]

        dpl_filter = st.selectbox(
            "DPL filtras",
            [
                "Visi",
                "Tik sandoriai per DPL",
                "Tik ne DPL",
            ],
            key="mgr_dpl_filter",
        )

        if dpl_filter == "Tik sandoriai per DPL":
            df = df[df["is_dpl_period"] == True]
        elif dpl_filter == "Tik ne DPL":
            df = df[df["is_dpl_period"] == False]

    _show_tables(df)

    st.download_button(
        "⬇ Atsisiųsti CSV",
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name="vadovu_sandoriai.csv",
        mime="text/csv",
        use_container_width=True,
    )

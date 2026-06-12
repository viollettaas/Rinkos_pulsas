# -*- coding: utf-8 -*-
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from supabase_cache import load_manager_transactions_df


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
        "issuer", "person_name", "person_role", "isin", "instrument",
        "transaction_type", "venue", "parse_status", "price_quantity_note",
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
    issuers = df["issuer"].replace("", pd.NA).dropna().nunique() if "issuer" in df.columns else 0
    persons = df["person_name"].replace("", pd.NA).dropna().nunique() if "person_name" in df.columns else 0
    late = int(df["is_late_notification"].sum()) if "is_late_notification" in df.columns else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pranešimų / PDF", total)
    c2.metric("Emitentų", issuers)
    c3.metric("Asmenų", persons)
    c4.metric("Vėluojančių >3 d.", late)


def _show_tables(df: pd.DataFrame):
    st.subheader("1. Detali vadovų sandorių lentelė")

    detail_cols = [
        "published_date",
        "transaction_date_dt",
        "days_to_publish",
        "is_late_notification",
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
    st.dataframe(df[detail_cols], use_container_width=True, hide_index=True)

    st.subheader("2. Santrauka pagal emitentą")
    issuer_summary = (
        df.groupby("issuer", dropna=False)
        .agg(
            pranesimu_sk=("pdf_url", "count"),
            asmenu_sk=("person_name", pd.Series.nunique),
            sandorio_bendra_verte=("transaction_value", "sum"),
            veluojanciu_sk=("is_late_notification", "sum"),
            vid_dienu_iki_pranesimo=("days_to_publish", "mean"),
        )
        .reset_index()
        .sort_values(["pranesimu_sk", "sandorio_bendra_verte"], ascending=[False, False])
    )
    st.dataframe(issuer_summary, use_container_width=True, hide_index=True)

    st.subheader("3. Santrauka pagal asmenį")
    person_summary = (
        df.groupby(["issuer", "person_name"], dropna=False)
        .agg(
            pranesimu_sk=("pdf_url", "count"),
            sandorio_bendra_verte=("transaction_value", "sum"),
            veluojanciu_sk=("is_late_notification", "sum"),
            vid_dienu_iki_pranesimo=("days_to_publish", "mean"),
        )
        .reset_index()
        .sort_values(["pranesimu_sk", "sandorio_bendra_verte"], ascending=[False, False])
    )
    st.dataframe(person_summary, use_container_width=True, hide_index=True)

    st.subheader("4. Vėlavimo kontrolė")
    delay_cols = [
        "issuer", "person_name", "transaction_date_dt", "published_date",
        "days_to_publish", "is_late_notification", "transaction_type", "pdf_url"
    ]
    delay_cols = [c for c in delay_cols if c in df.columns]
    delay_df = df[delay_cols].sort_values("days_to_publish", ascending=False, na_position="last")
    st.dataframe(delay_df, use_container_width=True, hide_index=True)


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
                        Lentelėje papildomai skaičiuojamas dienų skaičius nuo sandorio datos iki pranešimo paskelbimo.
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
        st.markdown('<div class="sidebar-card-title">👔 Vadovų sandoriai</div>', unsafe_allow_html=True)

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

    raw_df = load_manager_transactions_df(manager_start_date, manager_end_date)
    df = prepare_manager_transactions_df(raw_df)

    if df.empty:
        st.info("Pasirinktu laikotarpiu vadovų sandorių duomenų nėra.")
        st.stop()

    _show_summary_cards(df)
    st.markdown("---")

    with st.expander("Filtrai", expanded=True):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            df = _apply_multiselect_filter(df, "issuer", "Emitentas")
        with c2:
            df = _apply_multiselect_filter(df, "person_name", "Vadovas / susijęs asmuo")
        with c3:
            df = _apply_multiselect_filter(df, "transaction_type", "Sandorio pobūdis")
        with c4:
            df = _apply_multiselect_filter(df, "parse_status", "Apdorojimo statusas")

        delay_filter = st.selectbox(
            "Vėlavimo filtras",
            ["Visi", "Tik vėluojantys >3 d.", "Tik nevėluojantys <=3 d.", "Be apskaičiuoto termino"],
            key="mgr_delay_filter",
        )
        if delay_filter == "Tik vėluojantys >3 d.":
            df = df[df["is_late_notification"] == True]
        elif delay_filter == "Tik nevėluojantys <=3 d.":
            df = df[(df["days_to_publish"].notna()) & (df["days_to_publish"] <= 3)]
        elif delay_filter == "Be apskaičiuoto termino":
            df = df[df["days_to_publish"].isna()]

    _show_tables(df)

    st.download_button(
        "⬇ Atsisiųsti CSV",
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name="vadovu_sandoriai.csv",
        mime="text/csv",
        use_container_width=True,
    )

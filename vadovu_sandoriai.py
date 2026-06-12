# -*- coding: utf-8 -*-

import re
import tempfile
from datetime import date, timedelta
from urllib.parse import urljoin

import pandas as pd
import pdfplumber
import requests
import streamlit as st
from bs4 import BeautifulSoup

from supabase_cache import (
    save_manager_transaction,
    load_manager_transactions_df,
)


def extract_number(value):
    if not value:
        return None
    value = str(value).replace(" ", "").replace(",", ".")
    match = re.search(r"-?\d+(\.\d+)?", value)
    return float(match.group(0)) if match else None


def extract_date(text):
    patterns = [
        r"Sandorio data[:\s]+(\d{4}-\d{2}-\d{2})",
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{4}\.\d{2}\.\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).replace(".", "-")
    return None


def first_match(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().split("\n")[0]
    return None


def parse_pdf_text(text):
    data = {
        "issuer": None,
        "lei": None,
        "person_name": None,
        "person_role": None,
        "isin": None,
        "instrument": None,
        "transaction_type": None,
        "price": None,
        "quantity": None,
        "transaction_date": extract_date(text),
        "venue": None,
        "raw_text": text,
        "parse_status": "parsed",
    }

    lei_match = re.search(r"\b([A-Z0-9]{20})\b", text)
    isin_match = re.search(r"\b([A-Z]{2}[A-Z0-9]{10})\b", text)

    if lei_match:
        data["lei"] = lei_match.group(1)
    if isin_match:
        data["isin"] = isin_match.group(1)

    data["person_name"] = first_match(text, [
        r"Vardas ir pavardė[:\s]+(.+)",
        r"Pranešėjo vardas ir pavardė[:\s]+(.+)",
        r"Name[:\s]+(.+)",
    ])

    data["person_role"] = first_match(text, [
        r"Pareigos[:\s]+(.+)",
        r"Statusas[:\s]+(.+)",
        r"Position[:\s]+(.+)",
    ])

    data["issuer"] = first_match(text, [
        r"Emitento pavadinimas[:\s]+(.+)",
        r"Emitentas[:\s]+(.+)",
        r"Issuer[:\s]+(.+)",
    ])

    data["instrument"] = first_match(text, [
        r"Finansinės priemonės[:\s]+(.+)",
        r"Priemonė[:\s]+(.+)",
        r"Instrument[:\s]+(.+)",
    ])

    data["transaction_type"] = first_match(text, [
        r"Sandorio pobūdis[:\s]+(.+)",
        r"Sandorio rūšis[:\s]+(.+)",
        r"Nature of the transaction[:\s]+(.+)",
        r"Transaction type[:\s]+(.+)",
    ])

    quantity_text = first_match(text, [
        r"Kiekis[:\s]+([\d\s,.]+)",
        r"Apimtis[:\s]+([\d\s,.]+)",
        r"Volume[:\s]+([\d\s,.]+)",
    ])

    price_text = first_match(text, [
        r"Kaina[:\s]+([\d\s,.]+)",
        r"Price[:\s]+([\d\s,.]+)",
    ])

    data["quantity"] = extract_number(quantity_text)
    data["price"] = extract_number(price_text)

    data["venue"] = first_match(text, [
        r"Sandorio vieta[:\s]+(.+)",
        r"Prekybos vieta[:\s]+(.+)",
        r"Venue[:\s]+(.+)",
    ])

    if not data["person_name"] or not data["isin"]:
        data["parse_status"] = "needs_review"

    return data


def download_and_extract_pdf(pdf_url):
    response = requests.get(pdf_url, timeout=30, verify=False)
    response.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(response.content)
        tmp.flush()

        text_parts = []
        with pdfplumber.open(tmp.name) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")

    return "\n".join(text_parts)


def get_crib_pdf_links(crib_url):
    response = requests.get(crib_url, timeout=30, verify=False)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text("\n", strip=True)

    title = soup.find("h1").get_text(strip=True) if soup.find("h1") else None

    published_at = None
    published_match = re.search(
        r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        page_text,
    )
    if published_match:
        published_at = published_match.group(1)

    category = None
    if "Pranešimai apie vadovų sandorius" in page_text:
        category = "Pranešimai apie vadovų sandorius"

    pdfs = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        label = link.get_text(" ", strip=True)

        if ".pdf" in href.lower() or ".pdf" in label.lower() or "attachment" in href.lower():
            pdfs.append({
                "pdf_name": label or href,
                "pdf_url": urljoin(crib_url, href),
            })

    unique = {}
    for pdf in pdfs:
        unique[pdf["pdf_url"]] = pdf

    return {
        "crib_url": crib_url,
        "crib_title": title,
        "crib_category": category,
        "published_at": published_at,
        "pdfs": list(unique.values()),
    }


def save_manager_transactions_from_crib(crib_url):
    meta = get_crib_pdf_links(crib_url)

    if meta["crib_category"] != "Pranešimai apie vadovų sandorius":
        return {
            "status": "skipped",
            "message": "Ne vadovų sandorių kategorija",
            "saved": 0,
        }

    saved_count = 0

    for pdf in meta["pdfs"]:
        try:
            text = download_and_extract_pdf(pdf["pdf_url"])
            parsed = parse_pdf_text(text)

            row = {
                "crib_url": meta["crib_url"],
                "crib_title": meta["crib_title"],
                "crib_category": meta["crib_category"],
                "published_at": meta["published_at"],
                "pdf_url": pdf["pdf_url"],
                "pdf_name": pdf["pdf_name"],
                **parsed,
            }

            save_manager_transaction(row)
            saved_count += 1

        except Exception as error:
            row = {
                "crib_url": meta["crib_url"],
                "crib_title": meta["crib_title"],
                "crib_category": meta["crib_category"],
                "published_at": meta["published_at"],
                "pdf_url": pdf["pdf_url"],
                "pdf_name": pdf["pdf_name"],
                "parse_status": f"failed: {error}",
            }
            save_manager_transaction(row)

    return {
        "status": "done",
        "saved": saved_count,
    }


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
                        Kiekvienas PDF saugomas kaip atskira eilutė Supabase lentelėje.
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
            "Nuo",
            value=date.today() - timedelta(days=30),
            key="manager_start_date",
        )

        manager_end_date = st.date_input(
            "Iki",
            value=date.today(),
            key="manager_end_date",
        )

        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown('<div class="sidebar-card">', unsafe_allow_html=True)
        st.markdown('<div class="sidebar-card-title">🔗 Naujas CRIB pranešimas</div>', unsafe_allow_html=True)

        crib_url = st.text_input(
            "CRIB nuoroda",
            placeholder="https://crib.lt/view/472887?lang=lt",
            key="manager_crib_url",
        )

        load_btn = st.button(
            "📥 Nuskaityti ir išsaugoti",
            type="primary",
            use_container_width=True,
            key="manager_load_btn",
        )

        st.markdown("</div>", unsafe_allow_html=True)

    if manager_start_date > manager_end_date:
        st.error("Data „Nuo“ negali būti vėlesnė už datą „Iki“.")
        st.stop()

    if load_btn:
        if not crib_url:
            st.warning("Įvesk CRIB pranešimo nuorodą.")
        else:
            try:
                with st.spinner("Nuskaitomi PDF dokumentai ir saugomi Supabase..."):
                    result = save_manager_transactions_from_crib(crib_url)

                if result.get("status") == "skipped":
                    st.warning(result.get("message", "Pranešimas praleistas."))
                else:
                    st.success(f"Išsaugota PDF eilučių: {result.get('saved', 0)}")

            except Exception as exc:
                st.error("Nepavyko nuskaityti arba išsaugoti vadovų sandorių.")
                st.exception(exc)

    df = load_manager_transactions_df(manager_start_date, manager_end_date)

    col1, col2, col3 = st.columns(3)
    col1.metric("🗓️ Nuo", str(manager_start_date))
    col2.metric("🗓️ Iki", str(manager_end_date))
    col3.metric("📄 PDF eilučių", len(df))

    st.markdown("---")

    if df.empty:
        st.info("Pasirinktu laikotarpiu vadovų sandorių duomenų nėra.")
        st.stop()

    issuer_filter = st.multiselect(
        "Emitentas",
        sorted(df["issuer"].dropna().unique()) if "issuer" in df.columns else [],
    )

    person_filter = st.multiselect(
        "Vadovas / susijęs asmuo",
        sorted(df["person_name"].dropna().unique()) if "person_name" in df.columns else [],
    )

    type_filter = st.multiselect(
        "Sandorio pobūdis",
        sorted(df["transaction_type"].dropna().unique()) if "transaction_type" in df.columns else [],
    )

    status_filter = st.multiselect(
        "Apdorojimo statusas",
        sorted(df["parse_status"].dropna().unique()) if "parse_status" in df.columns else [],
    )

    filtered = df.copy()

    if issuer_filter and "issuer" in filtered.columns:
        filtered = filtered[filtered["issuer"].isin(issuer_filter)]

    if person_filter and "person_name" in filtered.columns:
        filtered = filtered[filtered["person_name"].isin(person_filter)]

    if type_filter and "transaction_type" in filtered.columns:
        filtered = filtered[filtered["transaction_type"].isin(type_filter)]

    if status_filter and "parse_status" in filtered.columns:
        filtered = filtered[filtered["parse_status"].isin(status_filter)]

    show_cols = [
        "published_at",
        "issuer",
        "person_name",
        "person_role",
        "isin",
        "instrument",
        "transaction_type",
        "price",
        "quantity",
        "transaction_date",
        "venue",
        "parse_status",
        "pdf_url",
        "crib_url",
    ]

    available_cols = [col for col in show_cols if col in filtered.columns]

    st.subheader("Rezultatai")

    st.dataframe(
        filtered[available_cols],
        use_container_width=True,
        hide_index=True,
    )

    csv = filtered.to_csv(index=False).encode("utf-8-sig")

    st.download_button(
        "⬇ Atsisiųsti CSV",
        data=csv,
        file_name="vadovu_sandoriai.csv",
        mime="text/csv",
        use_container_width=True,
    )

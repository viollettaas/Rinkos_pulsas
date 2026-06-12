# -*- coding: utf-8 -*-

import re
import tempfile
import requests
import pdfplumber
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from supabase_cache import save_manager_transaction


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
        page_text
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
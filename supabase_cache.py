# -*- coding: utf-8 -*-
import hashlib
from datetime import date

import httpx
import pandas as pd
import streamlit as st


def normalize_for_key(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def make_unique_key(source, url=None, title=None, company=None, published_at=None):
    if url:
        base = f"{source}|url|{normalize_for_key(url)}"
    else:
        base = (
            f"{source}|"
            f"{normalize_for_key(company)}|"
            f"{normalize_for_key(title)}|"
            f"{normalize_for_key(published_at)}"
        )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _supabase_headers():
    key = st.secrets["SUPABASE_KEY"]
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _supabase_rest_url(table_name: str) -> str:
    base_url = st.secrets["SUPABASE_URL"].rstrip("/")
    return f"{base_url}/rest/v1/{table_name}"


def _http_client():
    return httpx.Client(verify=False, timeout=60)


def save_news_df(df: pd.DataFrame, source: str):
    if df is None or df.empty:
        return 0

    rows = []

    for _, r in df.iterrows():
        if source == "crib":
            company = r.get("Bendrovė", "")
            company_norm = r.get("Bendrovė_norm", "")
            category = r.get("Kategorija", "")
            title = r.get("Pilna_antraštė", "") or r.get("Naujiena", "")
            url = r.get("Nuoroda", "")
            published_at = r.get("Published_dt", None)
            content = r.get("Pilnas_tekstas", "")

        elif source == "vz":
            company = ""
            company_norm = ""
            category = "Verslo žinios"
            title = r.get("Antraštė", "")
            url = r.get("Nuoroda", "")
            published_at = r.get("Data", None)
            content = r.get("Pilnas_tekstas", "")

        elif source == "nasdaq":
            company = r.get("Nasdaq_bendrovė", "")
            company_norm = ""
            category = "Nasdaq Baltic (Vilnius)"
            title = r.get("Nasdaq_pilna_antraštė", "") or r.get("Nasdaq_antraštė", "")
            url = r.get("Nasdaq_nuoroda", "")
            published_at = r.get("__dt", None)
            content = r.get("Nasdaq_pilnas_tekstas", "")

        else:
            continue

        if pd.isna(published_at):
            published_at = None

        if published_at is not None:
            published_at = pd.to_datetime(published_at).isoformat()

        unique_key = make_unique_key(
            source=source,
            url=url,
            title=title,
            company=company,
            published_at=published_at,
        )

        rows.append({
            "source": source,
            "company": str(company) if company is not None else "",
            "company_norm": str(company_norm) if company_norm is not None else "",
            "ticker": "",
            "category": str(category) if category is not None else "",
            "title": str(title) if title is not None else "",
            "url": str(url) if url is not None else "",
            "published_at": published_at,
            "content": str(content) if content is not None else "",
            "market": "VLN",
            "language": "lt",
            "unique_key": unique_key,
        })

    if not rows:
        return 0

    url = _supabase_rest_url("market_news")
    inserted = 0

    with _http_client() as client:
        for row in rows:
            response = client.post(
                url,
                headers={
                    **_supabase_headers(),
                    "Prefer": "resolution=ignore-duplicates,return=minimal",
                },
                json=row,
            )

            if response.status_code in (200, 201, 204):
                inserted += 1
            elif response.status_code == 409:
                pass
            else:
                raise RuntimeError(
                    f"Supabase įrašymo klaida: {response.status_code} - {response.text}"
                )

    return inserted


def load_news_df(source: str, start_date: date, end_date: date) -> pd.DataFrame:
    start_iso = f"{start_date}T00:00:00"
    end_iso = f"{end_date}T23:59:59"

    url = _supabase_rest_url("market_news")
    params = {
        "select": "*",
        "source": f"eq.{source}",
        "published_at": [f"gte.{start_iso}", f"lte.{end_iso}"],
        "order": "published_at.desc",
    }

    with _http_client() as client:
        response = client.get(
            url,
            headers=_supabase_headers(),
            params=params,
        )
        response.raise_for_status()
        data = response.json()

    return pd.DataFrame(data or [])


def log_scrape(source, date_from, date_to, status, records_found=0, error_message=None):
    url = _supabase_rest_url("market_news_scrape_log")

    row = {
        "source": source,
        "date_from": str(date_from),
        "date_to": str(date_to),
        "status": status,
        "records_found": records_found,
        "error_message": error_message,
    }

    with _http_client() as client:
        response = client.post(
            url,
            headers=_supabase_headers(),
            json=row,
        )

        if response.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"Supabase log įrašymo klaida: {response.status_code} - {response.text}"
            )
            def save_manager_transaction(row: dict):
    url = _supabase_rest_url("manager_transactions")

    with _http_client() as client:
        response = client.post(
            url,
            headers={
                **_supabase_headers(),
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=row,
        )

        if response.status_code in (200, 201, 204):
            return True

        raise RuntimeError(
            f"Supabase vadovų sandorio įrašymo klaida: "
            f"{response.status_code} - {response.text}"
        )


def load_manager_transactions_df(start_date: date, end_date: date) -> pd.DataFrame:
    start_iso = f"{start_date}T00:00:00"
    end_iso = f"{end_date}T23:59:59"

    url = _supabase_rest_url("manager_transactions")

    params = {
        "select": "*",
        "published_at": [f"gte.{start_iso}", f"lte.{end_iso}"],
        "order": "published_at.desc",
    }

    with _http_client() as client:
        response = client.get(
            url,
            headers=_supabase_headers(),
            params=params,
        )
        response.raise_for_status()
        data = response.json()

    return pd.DataFrame(data or [])

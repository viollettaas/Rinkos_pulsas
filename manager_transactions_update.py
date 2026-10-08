# -*- coding: utf-8 -*-
"""Papildo manager_transactions iš jau sukauptų CRIB market_news įrašų."""

from datetime import date, timedelta
import re

import pandas as pd

from supabase_cache import load_news_df, _supabase_headers, _supabase_rest_url, _http_client
from vadovu_sandoriai import _init_driver, save_manager_transactions_from_crib_selenium


def _is_manager_notice(row) -> bool:
    text = " ".join(
        str(row.get(col, "") or "")
        for col in ("category", "title", "content")
    ).lower()
    return bool(
        "pranešimai apie vadovų sandorius" in text
        or "pranesimai apie vadovu sandorius" in text
        or "notifications on transactions concluded by managers" in text
        or re.search(r"\bmanagers?['’]?\s+transactions?\b", text, flags=re.I)
        or "vadovų sandori" in text
        or "vadovu sandori" in text
    )


def _last_manager_notice_date():
    """Paskutinio išsaugoto manager_transactions pranešimo data."""
    try:
        url = _supabase_rest_url("manager_transactions")
        params = {
            "select": "published_at",
            "published_at": "not.is.null",
            "order": "published_at.desc",
            "limit": "1",
        }
        with _http_client() as client:
            response = client.get(url, headers=_supabase_headers(), params=params)
            response.raise_for_status()
            rows = response.json() or []
        if not rows:
            return None
        value = pd.to_datetime(rows[0].get("published_at"), errors="coerce")
        return None if pd.isna(value) else value.date()
    except Exception:
        return None


def _load_new_manager_notices(start_date):
    df = load_news_df("crib", start_date, date.today())
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    for col in ("category", "title", "content", "url", "published_at"):
        if col not in df.columns:
            df[col] = ""
    df = df[df.apply(_is_manager_notice, axis=1)].copy()
    if df.empty:
        return df
    df["published_at_dt"] = pd.to_datetime(df["published_at"], errors="coerce")
    return (
        df[df["url"].fillna("").astype(str).str.strip().ne("")]
        .sort_values("published_at_dt", ascending=True)
        .drop_duplicates(subset=["url"], keep="last")
        .reset_index(drop=True)
    )


def update_manager_transactions_from_recent_crib(
    days_back: int = 365,
    max_messages: int = 1000,
    headless: bool = True,
    progress=None,
) -> dict:
    """Papildo visus vadovų sandorius nuo paskutinio DB įrašo."""
    last_date = _last_manager_notice_date()
    start_date = last_date - timedelta(days=1) if last_date else date.today() - timedelta(days=days_back)
    stats = {
        "sync_from_date": start_date.isoformat(),
        "manager_messages_found": 0,
        "manager_messages_processed": 0,
        "manager_transactions_saved": 0,
        "manager_transactions_errors": 0,
    }

    notices = _load_new_manager_notices(start_date).head(max_messages)
    stats["manager_messages_found"] = len(notices)
    if notices.empty:
        return stats

    driver = _init_driver(headless=headless)
    try:
        for _, notice in notices.iterrows():
            url = str(notice.get("url") or "").strip()
            if not url:
                continue
            stats["manager_messages_processed"] += 1
            try:
                if progress:
                    progress(f"Nuskaitomas vadovų sandorio PDF: {url}")
                saved = save_manager_transactions_from_crib_selenium(
                    driver=driver,
                    crib_url=url,
                    published_at=notice.get("published_at"),
                    crib_title=str(notice.get("title") or ""),
                    crib_category=str(notice.get("category") or ""),
                )
                stats["manager_transactions_saved"] += int(saved or 0)
            except Exception:
                stats["manager_transactions_errors"] += 1
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return stats

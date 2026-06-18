# -*- coding: utf-8 -*-
"""
issuer_cache.py

Emitentų sąrašo cache Supabase duomenų bazėje.
Naudojama VŽ atnaujinimui, kad nereikėtų kiekvieną kartą siųstis Nasdaq statistics.

Reikalinga Supabase lentelė: market_issuers
Unikali kolona: unique_key
"""

import hashlib
from datetime import date, datetime, timezone

import pandas as pd

from supabase_cache import _supabase_headers, _supabase_rest_url, _http_client


def _norm(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm_key(value) -> str:
    return _norm(value).lower()


def _find_col(df: pd.DataFrame, candidates):
    if df is None or df.empty:
        return None

    lower_map = {str(c).strip().lower(): c for c in df.columns}

    for c in candidates:
        key = str(c).strip().lower()
        if key in lower_map:
            return lower_map[key]

    for col in df.columns:
        col_l = str(col).strip().lower()
        for c in candidates:
            if str(c).strip().lower() in col_l:
                return col

    return None


def _issuer_unique_key(source: str, issuer: str, market: str = "VLN") -> str:
    base = f"{source}|{market}|{_norm_key(issuer)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def build_issuer_df_from_stat_df(df_stat: pd.DataFrame) -> pd.DataFrame:
    """
    Iš Nasdaq statistics df suformuoja emitentų sąrašą.
    Grąžina DataFrame su stulpeliu Bendrovė, kurį gali naudoti vz_scrape_full().
    """
    if df_stat is None or df_stat.empty:
        return pd.DataFrame(columns=["Bendrovė", "Trumpinys", "Sąrašas/segmentas"])

    company_col = _find_col(df_stat, ["Bendrovė", "Bendrove", "Emitentas", "Issuer", "Company"])
    ticker_col = _find_col(df_stat, ["Trumpinys", "Ticker", "Symbol", "ISIN"])
    segment_col = _find_col(df_stat, ["Sąrašas/segmentas", "Sarasas/segmentas", "Segmentas", "List", "Market segment"])

    if company_col is None:
        return pd.DataFrame(columns=["Bendrovė", "Trumpinys", "Sąrašas/segmentas"])

    out = pd.DataFrame()
    out["Bendrovė"] = df_stat[company_col].fillna("").astype(str).str.strip()
    out["Trumpinys"] = df_stat[ticker_col].fillna("").astype(str).str.strip() if ticker_col else ""
    out["Sąrašas/segmentas"] = df_stat[segment_col].fillna("").astype(str).str.strip() if segment_col else ""

    out = out[out["Bendrovė"] != ""].drop_duplicates(subset=["Bendrovė"]).reset_index(drop=True)
    return out


def save_issuer_list_from_stat_df(df_stat: pd.DataFrame, source: str = "nasdaq_statistics", market: str = "VLN") -> int:
    """
    Išsaugo arba atnaujina emitentų sąrašą Supabase market_issuers lentelėje.

    Svarbu: pildome ir issuer, ir company laukus, nes ankstesnėje lentelės
    schemoje company galėjo būti NOT NULL.
    """
    issuer_df = build_issuer_df_from_stat_df(df_stat)
    if issuer_df.empty:
        return 0

    today = date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    rows = []

    for _, r in issuer_df.iterrows():
        issuer = _norm(r.get("Bendrovė", ""))
        if not issuer:
            continue

        issuer_norm = _norm_key(issuer)
        unique_key = _issuer_unique_key(source, issuer, market)

        rows.append({
            "source": source,
            "market": market,
            "issuer": issuer,
            "issuer_norm": issuer_norm,
            "company": issuer,
            "company_norm": issuer_norm,
            "ticker": _norm(r.get("Trumpinys", "")),
            "segment": _norm(r.get("Sąrašas/segmentas", "")),
            "last_seen_date": today,
            "updated_at": now,
            "unique_key": unique_key,
        })

    if not rows:
        return 0

    url = _supabase_rest_url("market_issuers")
    saved = 0

    with _http_client() as client:
        for row in rows:
            response = client.post(
                url,
                headers={
                    **_supabase_headers(),
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
                params={"on_conflict": "unique_key"},
                json=row,
            )
            if response.status_code in (200, 201, 204):
                saved += 1
            else:
                raise RuntimeError(
                    f"Supabase emitentų sąrašo įrašymo klaida: {response.status_code} - {response.text}"
                )

    return saved


def load_issuer_df(source: str = "nasdaq_statistics", market: str = "VLN") -> pd.DataFrame:
    """
    Užkrauna emitentų sąrašą iš Supabase market_issuers.
    Grąžina struktūrą su Bendrovė stulpeliu, suderinamą su vz_scrape_full().
    """
    url = _supabase_rest_url("market_issuers")
    params = {
        "select": "issuer,company,ticker,segment,last_seen_date,updated_at",
        "source": f"eq.{source}",
        "market": f"eq.{market}",
        "order": "issuer.asc",
    }

    with _http_client() as client:
        response = client.get(url, headers=_supabase_headers(), params=params)
        response.raise_for_status()
        data = response.json() or []

    if not data:
        return pd.DataFrame(columns=["Bendrovė", "Trumpinys", "Sąrašas/segmentas"])

    df = pd.DataFrame(data)
    issuer_series = df.get("issuer", pd.Series(dtype=str)).fillna("").astype(str)

    if issuer_series.str.strip().eq("").all() and "company" in df.columns:
        issuer_series = df["company"].fillna("").astype(str)

    out = pd.DataFrame({
        "Bendrovė": issuer_series,
        "Trumpinys": df.get("ticker", pd.Series(dtype=str)).fillna("").astype(str),
        "Sąrašas/segmentas": df.get("segment", pd.Series(dtype=str)).fillna("").astype(str),
    })
    out = out[out["Bendrovė"].str.strip() != ""].drop_duplicates(subset=["Bendrovė"]).reset_index(drop=True)
    return out


def get_issuer_cache_info(source: str = "nasdaq_statistics", market: str = "VLN") -> dict:
    """Trumpa informacija Streamlit žinutei."""
    try:
        df = load_issuer_df(source=source, market=market)
        return {"count": len(df), "ok": True}
    except Exception as exc:
        return {"count": 0, "ok": False, "error": str(exc)}

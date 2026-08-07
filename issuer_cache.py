# -*- coding: utf-8 -*-
"""
issuer_cache.py

Emitentų sąrašo cache Supabase duomenų bazėje.
Naudojama VŽ atnaujinimui, kad nereikėtų kiekvieną kartą
siųstis Nasdaq statistics.

Reikalinga Supabase lentelė: market_issuers
Unikali kolona: unique_key
"""

import hashlib
from datetime import date, datetime, timezone

import pandas as pd

from supabase_cache import (
    _supabase_headers,
    _supabase_rest_url,
    _http_client,
)


def _norm(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _norm_key(value) -> str:
    return _norm(value).lower()


def _find_col(df: pd.DataFrame, candidates):
    if df is None or df.empty:
        return None

    lower_map = {
        str(c).strip().lower(): c
        for c in df.columns
    }

    # Tikslus stulpelio pavadinimo sutapimas
    for c in candidates:
        key = str(c).strip().lower()

        if key in lower_map:
            return lower_map[key]

    # Dalinis sutapimas
    for col in df.columns:
        col_l = str(col).strip().lower()

        for c in candidates:
            candidate = str(c).strip().lower()

            if candidate in col_l:
                return col

    return None


def _issuer_unique_key(
    source: str,
    issuer: str,
    market: str = "VLN",
) -> str:
    """
    Dabartinė logika:
    vienas unique_key vienam emitentui.

    Kol kas nekeičiam šios logikos,
    kad nesugadintume kitų Rinkos pulso dalių.
    """

    base = (
        f"{source}|"
        f"{market}|"
        f"{_norm_key(issuer)}"
    )

    return hashlib.sha256(
        base.encode("utf-8")
    ).hexdigest()


def build_issuer_df_from_stat_df(
    df_stat: pd.DataFrame,
) -> pd.DataFrame:
    """
    Iš Nasdaq statistics DataFrame suformuoja emitentų sąrašą.

    Nasdaq Statistics VLN lape:
    E stulpelis = ISIN.

    Pandas pozicija:
    E = 5-as stulpelis = iloc[:, 4]

    Grąžina:
        Bendrovė
        Trumpinys
        ISIN
        Sąrašas/segmentas
    """

    output_columns = [
        "Bendrovė",
        "Trumpinys",
        "ISIN",
        "Sąrašas/segmentas",
    ]

    if df_stat is None or df_stat.empty:
        return pd.DataFrame(
            columns=output_columns
        )

    company_col = _find_col(
        df_stat,
        [
            "Bendrovė",
            "Bendrove",
            "Emitentas",
            "Issuer",
            "Company",
        ],
    )

    ticker_col = _find_col(
        df_stat,
        [
            "Trumpinys",
            "Ticker",
            "Symbol",
        ],
    )

    segment_col = _find_col(
        df_stat,
        [
            "Sąrašas/segmentas",
            "Sarasas/segmentas",
            "Segmentas",
            "List",
            "Market segment",
        ],
    )

    if company_col is None:
        return pd.DataFrame(
            columns=output_columns
        )

    out = pd.DataFrame()

    # Emitentas / bendrovė
    out["Bendrovė"] = (
        df_stat[company_col]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    # Ticker
    if ticker_col:
        out["Trumpinys"] = (
            df_stat[ticker_col]
            .fillna("")
            .astype(str)
            .str.strip()
        )
    else:
        out["Trumpinys"] = ""

    # ----------------------------------------
    # ISIN
    # Nasdaq Statistics faile:
    # E stulpelis = index 4
    # ----------------------------------------
    if len(df_stat.columns) >= 5:

        out["ISIN"] = (
            df_stat.iloc[:, 4]
            .fillna("")
            .astype(str)
            .str.strip()
        )

    else:

        out["ISIN"] = ""

    # Segmentas
    if segment_col:
        out["Sąrašas/segmentas"] = (
            df_stat[segment_col]
            .fillna("")
            .astype(str)
            .str.strip()
        )
    else:
        out["Sąrašas/segmentas"] = ""

    # Pašaliname tuščius emitentus
    out = out[
        out["Bendrovė"] != ""
    ]

    # Kol kas paliekame vieną eilutę bendrovei,
    # kaip buvo dabartinėje Rinkos pulso logikoje.
    out = (
        out
        .drop_duplicates(
            subset=["Bendrovė"]
        )
        .reset_index(drop=True)
    )

    return out


def save_issuer_list_from_stat_df(
    df_stat: pd.DataFrame,
    source: str = "nasdaq_statistics",
    market: str = "VLN",
) -> int:
    """
    Išsaugo arba atnaujina emitentų sąrašą
    Supabase market_issuers lentelėje.

    Įrašomi:
        issuer
        company
        ticker
        isin
        segment
        last_seen_date
        updated_at
        unique_key
    """

    issuer_df = build_issuer_df_from_stat_df(
        df_stat
    )

    if issuer_df.empty:
        return 0

    today = date.today().isoformat()

    now = (
        datetime.now(timezone.utc)
        .isoformat()
    )

    rows = []

    for _, r in issuer_df.iterrows():

        issuer = _norm(
            r.get(
                "Bendrovė",
                "",
            )
        )

        if not issuer:
            continue

        issuer_norm = _norm_key(
            issuer
        )

        ticker = _norm(
            r.get(
                "Trumpinys",
                "",
            )
        )

        isin = _norm(
            r.get(
                "ISIN",
                "",
            )
        )

        segment = _norm(
            r.get(
                "Sąrašas/segmentas",
                "",
            )
        )

        unique_key = _issuer_unique_key(
            source=source,
            issuer=issuer,
            market=market,
        )

        rows.append(
            {
                "source": source,
                "market": market,

                "issuer": issuer,
                "issuer_norm": issuer_norm,

                # Paliekame company dėl
                # suderinamumo su esama DB schema
                "company": issuer,
                "company_norm": issuer_norm,

                "ticker": ticker,

                # NAUJAS LAUKAS
                "isin": isin,

                "segment": segment,

                "last_seen_date": today,
                "updated_at": now,

                "unique_key": unique_key,
            }
        )

    if not rows:
        return 0

    url = _supabase_rest_url(
        "market_issuers"
    )

    saved = 0

    with _http_client() as client:

        for row in rows:

            response = client.post(
                url,
                headers={
                    **_supabase_headers(),
                    "Prefer": (
                        "resolution=merge-duplicates,"
                        "return=minimal"
                    ),
                },
                params={
                    "on_conflict": "unique_key"
                },
                json=row,
            )

            if response.status_code in (
                200,
                201,
                204,
            ):

                saved += 1

            else:

                raise RuntimeError(
                    "Supabase emitentų sąrašo "
                    "įrašymo klaida: "
                    f"{response.status_code} - "
                    f"{response.text}"
                )

    return saved


def load_issuer_df(
    source: str = "nasdaq_statistics",
    market: str = "VLN",
) -> pd.DataFrame:
    """
    Užkrauna emitentų sąrašą
    iš Supabase market_issuers.

    Grąžina struktūrą:
        Bendrovė
        Trumpinys
        ISIN
        Sąrašas/segmentas
    """

    url = _supabase_rest_url(
        "market_issuers"
    )

    params = {
        "select": (
            "issuer,"
            "company,"
            "ticker,"
            "isin,"
            "segment,"
            "last_seen_date,"
            "updated_at"
        ),

        "source": f"eq.{source}",
        "market": f"eq.{market}",

        "order": "issuer.asc",
    }

    with _http_client() as client:

        response = client.get(
            url,
            headers=_supabase_headers(),
            params=params,
        )

        response.raise_for_status()

        data = response.json() or []

    output_columns = [
        "Bendrovė",
        "Trumpinys",
        "ISIN",
        "Sąrašas/segmentas",
    ]

    if not data:
        return pd.DataFrame(
            columns=output_columns
        )

    df = pd.DataFrame(data)

    issuer_series = (
        df
        .get(
            "issuer",
            pd.Series(dtype=str),
        )
        .fillna("")
        .astype(str)
    )

    # Jei senuose įrašuose issuer buvo tuščias,
    # naudojame company
    if (
        issuer_series
        .str
        .strip()
        .eq("")
        .all()
        and "company" in df.columns
    ):

        issuer_series = (
            df["company"]
            .fillna("")
            .astype(str)
        )

    out = pd.DataFrame(
        {
            "Bendrovė":
                issuer_series,

            "Trumpinys":
                df.get(
                    "ticker",
                    pd.Series(dtype=str),
                )
                .fillna("")
                .astype(str),

            "ISIN":
                df.get(
                    "isin",
                    pd.Series(dtype=str),
                )
                .fillna("")
                .astype(str),

            "Sąrašas/segmentas":
                df.get(
                    "segment",
                    pd.Series(dtype=str),
                )
                .fillna("")
                .astype(str),
        }
    )

    out = out[
        out["Bendrovė"]
        .str
        .strip()
        != ""
    ]

    out = (
        out
        .drop_duplicates(
            subset=["Bendrovė"]
        )
        .reset_index(drop=True)
    )

    return out


def get_issuer_cache_info(
    source: str = "nasdaq_statistics",
    market: str = "VLN",
) -> dict:
    """
    Trumpa informacija Streamlit žinutei.
    """

    try:

        df = load_issuer_df(
            source=source,
            market=market,
        )

        return {
            "count": len(df),
            "ok": True,
        }

    except Exception as exc:

        return {
            "count": 0,
            "ok": False,
            "error": str(exc),
        }

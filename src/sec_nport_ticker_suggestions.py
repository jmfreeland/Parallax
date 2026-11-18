"""Find bond ticker identifiers reported in SEC N-PORT filings."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any
import xml.etree.ElementTree as ET

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "data" / "reference" / "sec_nport_bond_ticker_cache.csv"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"
_SEC_REQUEST_LOCK = threading.Lock()
_LAST_SEC_REQUEST_AT = 0.0
_MIN_SEC_REQUEST_INTERVAL_SECONDS = 0.12
PARSER_VERSION = "2"
CACHE_COLUMNS = [
    "isin", "cusip", "suggested_bond_ticker", "supporting_filings",
    "latest_filing_date", "suggestion_source", "suggestion_status", "checked_at_utc",
    "parser_version",
]


def _cusip_from_isin(isin: str) -> str:
    value = str(isin).strip().upper()
    return value[2:11] if value.startswith("US") and len(value) == 12 else ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _sec_get(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    """Make a fair-access SEC request, retrying transient throttling/server errors."""
    global _LAST_SEC_REQUEST_AT
    for attempt in range(5):
        with _SEC_REQUEST_LOCK:
            wait = _MIN_SEC_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _LAST_SEC_REQUEST_AT)
            if wait > 0:
                time.sleep(wait)
            _LAST_SEC_REQUEST_AT = time.monotonic()
        response = session.get(url, **kwargs)
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == 4:
            response.raise_for_status()
            return response
        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = min(30.0, max(1.0, float(retry_after)))
        except ValueError:
            delay = min(16.0, 1.0 * (2 ** attempt))
        time.sleep(delay)
    raise RuntimeError("SEC request retry loop ended unexpectedly")


def _filing_identity(hit: dict[str, Any]) -> tuple[str, str, str]:
    source = hit.get("_source", {})
    accession = str(source.get("adsh") or hit.get("_id") or "").replace("-", "")
    ciks = source.get("ciks") or source.get("cik") or []
    if isinstance(ciks, str):
        ciks = [ciks]
    cik = str(ciks[0]) if ciks else ""
    filing_date = str(source.get("file_date") or source.get("filed_at") or "")
    return accession, cik, filing_date


def _filing_xml_url(session: requests.Session, cik: str, accession: str) -> str:
    cik_number = str(int(cik))
    index_url = f"{ARCHIVES_URL}/{cik_number}/{accession}/index.json"
    response = _sec_get(session, index_url, timeout=(5, 25))
    items = response.json().get("directory", {}).get("item", [])
    candidates = [
        item.get("name", "") for item in items
        if item.get("name", "").casefold().endswith(".xml")
        and ("primary_doc" in item.get("name", "").casefold()
             or "nport" in item.get("name", "").casefold())
    ]
    if not candidates:
        raise ValueError("No N-PORT XML document was listed in the filing index")
    name = sorted(candidates, key=lambda value: ("primary_doc" not in value.casefold(), len(value)))[0]
    return f"{ARCHIVES_URL}/{cik_number}/{accession}/{name}"


def _tickers_for_cusip(
    cusip: str,
    session: requests.Session,
    *,
    end_date: date,
    max_filings: int,
) -> list[tuple[str, str]]:
    start_date = end_date - timedelta(days=365 * 4)
    response = _sec_get(
        session,
        SEARCH_URL,
        params={
            "q": cusip,
            "forms": "NPORT-P",
            "dateRange": "custom",
            "startdt": start_date.isoformat(),
            "enddt": end_date.isoformat(),
            "size": 100,
        },
        timeout=(5, 30),
    )
    hits = response.json().get("hits", {}).get("hits", [])
    filings = [_filing_identity(hit) for hit in hits]
    filings = sorted(
        {item for item in filings if item[0] and item[1]},
        key=lambda item: item[2],
        reverse=True,
    )[:max_filings]
    matches: list[tuple[str, str]] = []
    for accession, cik, filing_date in filings:
        try:
            xml_url = _filing_xml_url(session, cik, accession)
            xml_response = _sec_get(session, xml_url, timeout=(5, 45))
            root = ET.fromstring(xml_response.content)
        except (requests.RequestException, ET.ParseError, ValueError):
            # One unavailable filing should not discard evidence in the other
            # recent filings for this CUSIP.
            continue
        for investment in root.iter():
            if _local_name(investment.tag) != "invstorsec":
                continue
            investment_cusips = {
                (child.text or "").strip().upper()
                for child in investment.iter()
                if _local_name(child.tag) == "cusip"
            }
            if cusip not in investment_cusips:
                continue
            for child in investment.iter():
                if _local_name(child.tag) == "ticker":
                    ticker = str(child.attrib.get("value") or child.text or "").strip().upper()
                    if ticker:
                        matches.append((ticker, filing_date))
    return matches


def _resolve_isin(isin: str, user_agent: str, max_filings: int) -> dict[str, str]:
    cusip = _cusip_from_isin(isin)
    if not cusip:
        return {
            "isin": isin, "cusip": "", "suggested_bond_ticker": "",
            "supporting_filings": "0", "latest_filing_date": "",
            "suggestion_source": "SEC N-PORT", "suggestion_status": "no_us_cusip",
        }
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json, application/xml, text/xml, */*",
    })
    try:
        matches = _tickers_for_cusip(cusip, session, end_date=date.today(), max_filings=max_filings)
        counts: dict[str, int] = {}
        latest: dict[str, str] = {}
        for ticker, filing_date in matches:
            counts[ticker] = counts.get(ticker, 0) + 1
            latest[ticker] = max(latest.get(ticker, ""), filing_date)
        winner = sorted(counts, key=lambda ticker: (-counts[ticker], -int(re.sub(r"\D", "", latest[ticker]) or 0), ticker))[0] if counts else ""
        return {
            "isin": isin,
            "cusip": cusip,
            "suggested_bond_ticker": winner,
            "supporting_filings": str(counts.get(winner, 0)),
            "latest_filing_date": latest.get(winner, ""),
            "suggestion_source": "SEC N-PORT",
            "suggestion_status": "matched" if winner else "no_ticker_reported",
        }
    except (requests.RequestException, ET.ParseError, ValueError, KeyError, TypeError) as error:
        return {
            "isin": isin,
            "cusip": cusip,
            "suggested_bond_ticker": "",
            "supporting_filings": "0",
            "latest_filing_date": "",
            "suggestion_source": "SEC N-PORT",
            "suggestion_status": (
                f"lookup_failed: HTTP {error.response.status_code}"
                if isinstance(error, requests.HTTPError) and error.response is not None
                else f"lookup_failed: {type(error).__name__}"
            ),
        }
    finally:
        session.close()


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temp_path = Path(temp.name)
        frame.to_csv(temp, index=False, na_rep="")
    os.replace(temp_path, path)


def suggest_bond_tickers(
    isins: list[str],
    *,
    user_agent: str,
    cache_path: Path = DEFAULT_CACHE,
    max_filings: int = 3,
    on_progress: Any = None,
) -> pd.DataFrame:
    """Return cached or recent SEC N-PORT ticker suggestions for US ISINs."""
    if not user_agent.strip() or "@" not in user_agent:
        raise ValueError("A SEC User-Agent with a contact email is required")
    unique_isins = sorted({str(isin).strip().upper() for isin in isins if str(isin).strip()})
    cache_path = Path(cache_path)
    if cache_path.exists():
        cache = pd.read_csv(cache_path, dtype="string").fillna("")
    else:
        cache = pd.DataFrame(columns=CACHE_COLUMNS)
    for column in CACHE_COLUMNS:
        if column not in cache:
            cache[column] = ""
    cache = cache[CACHE_COLUMNS].drop_duplicates("isin", keep="last")
    checked_at = pd.to_datetime(cache["checked_at_utc"], errors="coerce", utc=True)
    still_fresh = (checked_at >= (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=90))) & ~cache[
        "suggestion_status"
    ].astype(str).str.startswith("lookup_failed")
    cached_isins = set(cache.loc[still_fresh, "isin"].astype(str))
    cached_isins &= set(cache.loc[still_fresh & cache["parser_version"].eq(PARSER_VERSION), "isin"].astype(str))
    pending = [isin for isin in unique_isins if isin not in cached_isins]
    resolved: list[dict[str, str]] = []
    if pending:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_resolve_isin, isin, user_agent.strip(), max_filings): isin
                for isin in pending
            }
            for index, future in enumerate(as_completed(futures), start=1):
                isin = futures[future]
                try:
                    record = future.result()
                except Exception as error:
                    record = {
                        "isin": isin, "cusip": _cusip_from_isin(isin),
                        "suggested_bond_ticker": "", "supporting_filings": "0",
                        "latest_filing_date": "", "suggestion_source": "SEC N-PORT",
                        "suggestion_status": f"lookup_failed: {type(error).__name__}",
                    }
                record["checked_at_utc"] = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
                record["parser_version"] = PARSER_VERSION
                resolved.append(record)
                if on_progress is not None:
                    on_progress(index, len(pending))
                if index % 25 == 0:
                    cache = pd.concat([cache, pd.DataFrame(resolved)], ignore_index=True)
                    cache = cache.drop_duplicates("isin", keep="last")[CACHE_COLUMNS]
                    _atomic_csv(cache, cache_path)
                    resolved.clear()
                time.sleep(0.05)
    if resolved:
        cache = pd.concat([cache, pd.DataFrame(resolved)], ignore_index=True)
    cache = cache.drop_duplicates("isin", keep="last")[CACHE_COLUMNS]
    _atomic_csv(cache, cache_path)
    result = pd.DataFrame({"isin": unique_isins}).merge(cache, on="isin", how="left")
    return result.fillna("")

"""Build a reviewable issuer-to-equity-ticker candidate file for the LQD snapshot.

GLEIF resolves one representative bond ISIN per issuer name to a legal entity.
The SEC company file then supplies exact legal-name ticker matches. Unique exact
issuer matches are applied automatically; direct-parent and ambiguous matches
are written as review candidates.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import requests

try:
    from .build_lqd_snapshot import normalize_issuer_name
except ImportError:
    from build_lqd_snapshot import normalize_issuer_name


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "data" / "snapshots" / "lqd_bonds.csv"
DEFAULT_CANDIDATES = ROOT / "data" / "issuer_ticker_candidates.csv"
DEFAULT_CACHE = ROOT / "data" / "reference" / "gleif_issuer_cache.csv"
DEFAULT_PARENT_CACHE = ROOT / "data" / "reference" / "gleif_parent_cache.csv"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
GLEIF_LEI_URL = "https://api.gleif.org/api/v1/lei-records"


def normalize_legal_name(name: str) -> str:
    """Normalize punctuation and equivalent suffix spellings without dropping legal form."""
    tokens = re.sub(r"[^A-Z0-9 ]", " ", str(name).upper()).split()
    suffix_aliases = {
        "INCORPORATED": "INC",
        "CORPORATION": "CORP",
        "COMPANY": "CO",
        "LIMITED": "LTD",
    }
    tokens = [suffix_aliases.get(token, token) for token in tokens]
    if tokens and tokens[0] == "THE":
        tokens.pop(0)
    return " ".join(tokens)


def _resolve_isin(isin: str) -> dict[str, str]:
    """Resolve one ISIN to a unique GLEIF LEI, with modest retry handling."""
    for attempt in range(5):
        response = requests.get(
            GLEIF_LEI_URL,
            params={"filter[isin]": isin, "page[size]": 10},
            headers={"User-Agent": "Parallax Observatory reference-data resolver"},
            timeout=30,
        )
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(min(2**attempt, 20))
            continue
        response.raise_for_status()
        records = response.json().get("data", [])
        leis = {record.get("id", "") for record in records if record.get("id")}
        if len(leis) != 1:
            return {"isin": isin, "lei": "", "gleif_legal_name": "", "gleif_status": "unmatched" if not leis else "ambiguous"}
        record = next(record for record in records if record.get("id") in leis)
        legal_name = record.get("attributes", {}).get("entity", {}).get("legalName", {}).get("name", "")
        return {"isin": isin, "lei": next(iter(leis)), "gleif_legal_name": legal_name, "gleif_status": "matched"}
    return {"isin": isin, "lei": "", "gleif_legal_name": "", "gleif_status": "request_failed"}


def _load_sec_tickers(user_agent: str) -> dict[str, list[dict[str, str]]]:
    response = requests.get(
        SEC_TICKERS_URL,
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        timeout=30,
    )
    response.raise_for_status()
    matches: dict[str, list[dict[str, str]]] = {}
    for cik, company_name, ticker, exchange in response.json().get("data", []):
        key = normalize_legal_name(company_name)
        if key:
            matches.setdefault(key, []).append(
                {"sec_cik": str(cik).zfill(10), "sec_company_name": company_name, "ticker_candidate": ticker, "exchange": exchange}
            )
    return matches


def _sec_match(sec: dict[str, list[dict[str, str]]], name: str) -> tuple[str, str, str, str, str, str]:
    """Return a unique ticker candidate only when the normalized name is unique."""
    hits = sec.get(normalize_legal_name(name), []) if name else []
    entities = {(hit["ticker_candidate"], hit["sec_cik"]) for hit in hits}
    if len(entities) == 1:
        hit = hits[0]
        return hit["ticker_candidate"], hit["sec_cik"], hit["sec_company_name"], hit["exchange"], "exact_name", ""
    if len(entities) > 1:
        names = " | ".join(sorted({hit["sec_company_name"] for hit in hits}))
        alternatives = " | ".join(sorted({f"{hit['ticker_candidate']}@{hit['exchange']}" for hit in hits}))
        return "", "", names, "", "ambiguous", alternatives
    return "", "", "", "", "none", ""


def _resolve_direct_parent(lei: str) -> dict[str, str]:
    """Look up the direct parent entity for a GLEIF LEI."""
    url = f"{GLEIF_LEI_URL}/{lei}/direct-parent"
    for attempt in range(5):
        response = requests.get(
            url,
            headers={"User-Agent": "Parallax Observatory reference-data resolver"},
            timeout=30,
        )
        if response.status_code == 404:
            return {"lei": lei, "parent_lei": "", "parent_legal_name": "", "parent_status": "no_parent_record"}
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(min(2**attempt, 20))
            continue
        response.raise_for_status()
        record = response.json().get("data") or {}
        parent_lei = record.get("id", "")
        name = record.get("attributes", {}).get("entity", {}).get("legalName", {}).get("name", "")
        return {"lei": lei, "parent_lei": parent_lei, "parent_legal_name": name, "parent_status": "matched" if parent_lei else "no_parent_record"}
    return {"lei": lei, "parent_lei": "", "parent_legal_name": "", "parent_status": "request_failed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--parent-cache", type=Path, default=DEFAULT_PARENT_CACHE)
    parser.add_argument("--gleif-only", action="store_true", help="resolve and save LEIs without querying SEC")
    parser.add_argument(
        "--sec-user-agent",
        default=os.environ.get("SEC_USER_AGENT", ""),
        help="SEC-required identifier including a real contact email; may use SEC_USER_AGENT env var",
    )
    args = parser.parse_args()
    if not args.gleif_only and (not args.sec_user_agent.strip() or "@" not in args.sec_user_agent):
        parser.error("Provide a real contact email in --sec-user-agent or SEC_USER_AGENT.")

    fixture = pd.read_csv(args.fixture, dtype={"isin": "string"})
    issuers = (
        fixture[["issuer_name", "isin"]]
        .dropna()
        .sort_values(["issuer_name", "isin"])
        .drop_duplicates("issuer_name")
        .reset_index(drop=True)
    )

    if args.cache.exists():
        cache = pd.read_csv(args.cache, dtype="string").fillna("")
    else:
        cache = pd.DataFrame(columns=["isin", "lei", "gleif_legal_name", "gleif_status"])
    cached_isins = set(cache["isin"].astype(str))
    failed_isins = (
        set(cache.loc[cache["gleif_status"] == "request_failed", "isin"].astype(str))
        if not cache.empty and "gleif_status" in cache
        else set()
    )
    pending = [
        isin for isin in issuers["isin"].astype(str)
        if isin not in cached_isins or isin in failed_isins
    ]
    if pending:
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        resolved: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_resolve_isin, isin): isin for isin in pending}
            for index, future in enumerate(as_completed(futures), start=1):
                isin = futures[future]
                try:
                    resolved.append(future.result())
                except requests.RequestException:
                    resolved.append({"isin": isin, "lei": "", "gleif_legal_name": "", "gleif_status": "request_failed"})
                if index % 50 == 0:
                    cache = pd.concat([cache, pd.DataFrame(resolved)], ignore_index=True).drop_duplicates("isin", keep="last")
                    cache.to_csv(args.cache, index=False)
                    resolved.clear()
                    print(f"Cached GLEIF lookups: {min(index, len(pending)):,}/{len(pending):,}")
        if resolved:
            cache = pd.concat([cache, pd.DataFrame(resolved)], ignore_index=True).drop_duplicates("isin", keep="last")
        cache.to_csv(args.cache, index=False)

    enriched = issuers.merge(cache, on="isin", how="left")
    if args.gleif_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        enriched.to_csv(args.output, index=False)
        print(f"Wrote {len(enriched):,} issuer-to-LEI rows to {args.output}")
        print(f"GLEIF LEIs matched: {enriched['lei'].fillna('').astype(bool).sum():,}")
        return

    sec = _load_sec_tickers(args.sec_user_agent.strip())
    direct_matches = enriched["gleif_legal_name"].fillna("").map(lambda name: _sec_match(sec, name)[4])
    needs_parent = enriched.loc[
        enriched["lei"].fillna("").ne("") & direct_matches.eq("none"), "lei"
    ].drop_duplicates().astype(str)
    if args.parent_cache.exists():
        parent_cache = pd.read_csv(args.parent_cache, dtype="string").fillna("")
    else:
        parent_cache = pd.DataFrame(columns=["lei", "parent_lei", "parent_legal_name", "parent_status"])
    cached_parent_leis = set(parent_cache["lei"].astype(str))
    pending_parents = [lei for lei in needs_parent if lei not in cached_parent_leis]
    if pending_parents:
        args.parent_cache.parent.mkdir(parents=True, exist_ok=True)
        parent_rows: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_resolve_direct_parent, lei): lei for lei in pending_parents}
            for future in as_completed(futures):
                lei = futures[future]
                try:
                    parent_rows.append(future.result())
                except requests.RequestException:
                    parent_rows.append({"lei": lei, "parent_lei": "", "parent_legal_name": "", "parent_status": "request_failed"})
        parent_cache = pd.concat([parent_cache, pd.DataFrame(parent_rows)], ignore_index=True).drop_duplicates("lei", keep="last")
        parent_cache.to_csv(args.parent_cache, index=False)
    enriched = enriched.merge(parent_cache, on="lei", how="left")

    candidates: list[dict[str, Any]] = []
    for row in enriched.to_dict(orient="records"):
        legal_name = row.get("gleif_legal_name") or ""
        ticker, cik, sec_name, exchange, direct_status, alternatives = _sec_match(sec, legal_name)
        candidate_entity_name = legal_name
        relationship = "issuer"
        status = "exact_issuer_entity_match_auto" if direct_status == "exact_name" else "no_exact_sec_match_review_required"
        if direct_status == "ambiguous":
            status = "ambiguous_issuer_name_review_required"
        elif direct_status == "none":
            parent_name = row.get("parent_legal_name") or ""
            parent_match = _sec_match(sec, parent_name)
            if parent_match[4] == "exact_name":
                ticker, cik, sec_name, exchange, _, alternatives = parent_match
                candidate_entity_name = parent_name
                relationship = "gleif_direct_parent"
                status = "direct_parent_candidate_review_required"
            elif parent_match[4] == "ambiguous":
                _, cik, sec_name, exchange, _, alternatives = parent_match
                candidate_entity_name = parent_name
                relationship = "gleif_direct_parent"
                status = "ambiguous_direct_parent_review_required"
        candidates.append({
            "issuer_name": row["issuer_name"],
            "representative_isin": row["isin"],
            "issuer_lei": row.get("lei", ""),
            "gleif_legal_name": legal_name,
            "gleif_status": row.get("gleif_status", "unmatched"),
            "direct_parent_lei": row.get("parent_lei", ""),
            "direct_parent_name": row.get("parent_legal_name", ""),
            "candidate_entity_name": candidate_entity_name,
            "candidate_relationship": relationship,
            "sec_cik": cik,
            "sec_company_name": sec_name,
            "ticker_candidate": ticker,
            "ticker_alternatives": alternatives,
            "exchange": exchange,
            "mapping_status": status,
            "reviewed_equity_ticker": "",
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    output = pd.DataFrame(candidates).sort_values("issuer_name")

    # Automatically persist only exact issuer-entity matches. Parent matches
    # and ambiguous listings remain candidates for a human decision.
    mapping_path = ROOT / "data" / "issuer_ticker_map.csv"
    mappings = pd.read_csv(mapping_path, dtype="string").fillna("") if mapping_path.exists() else pd.DataFrame(
        columns=["issuer_name", "equity_ticker", "mapping_source", "review_status"]
    )
    if "issuer_lei" not in mappings:
        mappings["issuer_lei"] = ""
    if "equity_exchange" not in mappings:
        mappings["equity_exchange"] = ""
    if "equity_country" not in mappings:
        mappings["equity_country"] = ""
    if "bond_ticker" not in mappings:
        mappings["bond_ticker"] = ""
    # Drop prior generated exact matches so improved name rules cannot leave stale assignments.
    mappings = mappings[mappings["review_status"].str.casefold() != "automated_exact_entity_match"].copy()
    existing_keys = {
        f"LEI:{row['issuer_lei']}" if row["issuer_lei"] else f"NAME:{normalize_issuer_name(row['issuer_name'])}"
        for row in mappings.to_dict(orient="records")
        if row["equity_ticker"].strip()
        and row["review_status"].casefold() in {"reviewed", "automated_exact_entity_match"}
    }
    # Attach the known LEI to name-based manual rows whenever the candidate name is unambiguous.
    lei_by_normalized_name: dict[str, set[str]] = {}
    for row in output.to_dict(orient="records"):
        key = normalize_issuer_name(row["issuer_name"])
        if row["issuer_lei"]:
            lei_by_normalized_name.setdefault(key, set()).add(row["issuer_lei"])
    for index, row in mappings.iterrows():
        key = normalize_issuer_name(row["issuer_name"])
        known_leis = lei_by_normalized_name.get(key, set())
        if not row["issuer_lei"] and len(known_leis) == 1:
            mappings.loc[index, "issuer_lei"] = next(iter(known_leis))
            existing_keys.discard(f"NAME:{key}")
            existing_keys.add(f"LEI:{mappings.loc[index, 'issuer_lei']}")
    exact = output[output["mapping_status"] == "exact_issuer_entity_match_auto"].copy()
    exact["identity_key"] = exact.apply(
        lambda row: f"LEI:{row['issuer_lei']}" if row["issuer_lei"] else f"NAME:{normalize_issuer_name(row['issuer_name'])}",
        axis=1,
    )
    for key, group in exact.groupby("identity_key"):
        tickers = set(group["ticker_candidate"].dropna().astype(str))
        if not key or len(tickers) != 1 or key in existing_keys:
            continue
        row = group.iloc[0]
        mappings.loc[len(mappings)] = {
            "issuer_name": row["issuer_name"],
            "issuer_lei": row["issuer_lei"],
            "equity_ticker": next(iter(tickers)),
            "mapping_source": f"GLEIF exact entity name {row['issuer_lei']}; SEC CIK {row['sec_cik']} ({row['exchange']})",
            "equity_exchange": row["exchange"],
            "review_status": "automated_exact_entity_match",
        }
        existing_keys.add(key)
    mappings.to_csv(mapping_path, index=False)

    lookup = {
        f"LEI:{row['issuer_lei']}" if row["issuer_lei"] else f"NAME:{normalize_issuer_name(row['issuer_name'])}": (
            row["equity_ticker"].strip().upper(),
            row["mapping_source"],
            row["review_status"].strip().casefold(),
            row.get("equity_exchange", ""),
            row.get("equity_country", ""),
        )
        for row in mappings.to_dict(orient="records")
        if row["equity_ticker"].strip()
        and row["review_status"].strip().casefold() in {"reviewed", "automated_exact_entity_match"}
    }
    candidate_leis = dict(zip(output["issuer_name"], output["issuer_lei"]))
    fixture["issuer_lei"] = fixture["issuer_name"].map(candidate_leis).fillna("")
    if "bond_ticker" not in fixture:
        fixture["bond_ticker"] = ""
    fixture["equity_ticker"] = fixture.apply(
        lambda row: lookup.get(f"LEI:{row['issuer_lei']}" if row["issuer_lei"] else f"NAME:{normalize_issuer_name(row['issuer_name'])}", ("", "", "", "", ""))[0],
        axis=1,
    )
    fixture["equity_exchange"] = fixture.apply(
        lambda row: lookup.get(f"LEI:{row['issuer_lei']}" if row["issuer_lei"] else f"NAME:{normalize_issuer_name(row['issuer_name'])}", ("", "", "", "", ""))[3],
        axis=1,
    )
    fixture["equity_country"] = fixture.apply(
        lambda row: lookup.get(f"LEI:{row['issuer_lei']}" if row["issuer_lei"] else f"NAME:{normalize_issuer_name(row['issuer_name'])}", ("", "", "", "", ""))[4],
        axis=1,
    )
    fixture["ticker_mapping_status"] = fixture["issuer_name"].map(
        lambda name: "reviewed" if lookup.get(f"LEI:{candidate_leis.get(name, '')}" if candidate_leis.get(name) else f"NAME:{normalize_issuer_name(name)}", ("", "", "", "", ""))[2] == "reviewed"
        else "exact_entity_match" if lookup.get(f"LEI:{candidate_leis.get(name, '')}" if candidate_leis.get(name) else f"NAME:{normalize_issuer_name(name)}", ("", "", "", "", ""))[2] == "automated_exact_entity_match"
        else "unmapped"
    )
    fixture.to_csv(args.fixture, index=False, na_rep="")
    output.to_csv(args.output, index=False)
    print(f"Wrote {len(output):,} issuer candidates to {args.output}")
    print(f"GLEIF LEIs matched: {output['issuer_lei'].astype(bool).sum():,}")
    print(f"Exact issuer matches applied: {(output['mapping_status'] == 'exact_issuer_entity_match_auto').sum():,}")
    print(f"Direct-parent candidates for review: {(output['candidate_relationship'] == 'gleif_direct_parent').sum():,}")
    print(f"Ambiguous issuer or parent matches: {output['mapping_status'].str.startswith('ambiguous').sum():,}")
    print(f"No exact or parent candidate: {(output['mapping_status'] == 'no_exact_sec_match_review_required').sum():,}")
    print(f"Fixture bonds with exact or reviewed ticker mapping: {fixture['equity_ticker'].astype(bool).sum():,}/{len(fixture):,}")


if __name__ == "__main__":
    main()

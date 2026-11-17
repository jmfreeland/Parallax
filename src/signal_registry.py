"""Catalog of research signals available in Parallax."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignalDefinition:
    signal_id: str
    name: str
    family: str
    construction: str
    inputs: str
    status: str
    validation: str


SIGNALS = (
    SignalDefinition(
        signal_id="nelson_siegel_residual",
        name="Nelson-Siegel ticker residual",
        family="Relative value",
        construction=(
            "Observed yield, G-spread, or clean price relative to a fitted issuer-ticker Nelson-Siegel curve; "
            "score direction is normalized so positive means cheap versus the fitted curve."
        ),
        inputs="TRACE yield/price or G-spread snapshot, maturity, manually mapped bond curve ticker",
        status="Implemented",
        validation="Prototype; no out-of-sample validation yet",
    ),
    SignalDefinition(
        signal_id="historical_partition_percentile",
        name="Historical partition yield percentile",
        family="Credit regime",
        construction=(
            "Partition median yield, G-spread, or clean price ranked against strictly prior values in a trailing window; "
            "score direction is normalized so higher values indicate cheaper levels."
        ),
        inputs="TRACE yield/price or G-spread snapshot and a selected bond-list partition field",
        status="Implemented",
        validation="Exploratory single-space signal; no out-of-sample validation yet",
    ),
    SignalDefinition(
        signal_id="historical_partition_zscore",
        name="Historical partition level z-score",
        family="Credit regime",
        construction=(
            "Partition median yield, G-spread, or clean price standardized against strictly prior values in a trailing window; "
            "score direction is normalized so higher values indicate cheaper levels."
        ),
        inputs="TRACE yield/price or G-spread snapshot and a selected bond-list partition field",
        status="Implemented",
        validation="Exploratory single-space signal; no out-of-sample validation yet",
    ),
)

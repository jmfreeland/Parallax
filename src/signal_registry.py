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
        construction="Observed bond yield minus fitted issuer-ticker Nelson-Siegel yield; positive is cheap vs curve.",
        inputs="TRACE yields, maturity, reviewed equity issuer ticker",
        status="Implemented",
        validation="Prototype; no out-of-sample validation yet",
    ),
    SignalDefinition(
        signal_id="historical_partition_percentile",
        name="Historical partition yield percentile",
        family="Credit regime",
        construction=(
            "Partition median latest TRACE yield ranked against strictly prior values in a trailing window; "
            "higher percentile indicates a high-yield regime."
        ),
        inputs="TRACE yields and a selected bond-list partition field",
        status="Implemented",
        validation="Exploratory yield proxy; not benchmark-relative spread; no out-of-sample validation yet",
    ),
)

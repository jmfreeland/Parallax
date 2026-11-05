# Parallax Observatory

Parallax is an internal Python platform for systematic research, risk, alpha, and portfolio analysis in US investment-grade credit. The Observatory is intended to make the evidence behind a view inspectable: source data, transformations, assumptions, model outputs, and portfolio implications should be traceable.

## Research frame

Keep four concepts distinct:

1. **Fundamental value** — an estimate grounded in issuer and security fundamentals.
2. **Latent expected trading level** — the market level we believe would prevail under ordinary conditions, distinct from both fundamentals and any single quote.
3. **Observed market data** — noisy, stale, sparse, or otherwise imperfect quotes and prints.
4. **Forecast return** — the expected change in value over a stated horizon, including carry, rolldown, spread movement, and relevant risks.

These are related but not interchangeable. Record the horizon, assumptions, uncertainty, and source for each estimate. Avoid treating a model-implied level as an observed price.

## v0.1: establish the data perimeter

Start with a pragmatic coverage audit of candidate OpenBB and FINRA sources before building research workflows. For each source and field, record availability, history, identifiers, update cadence, licensing/usage constraints, and known gaps. OpenBB is an integration path to investigate; access to any particular dataset, endpoint, or field is not yet verified.

Use a small provider interface so source implementations can be replaced without changing downstream research code. Normalize identifiers and timestamps, retain source and retrieval metadata, and make missingness explicit. When access is unavailable or a workflow needs deterministic examples, use clearly labelled synthetic data; never present it as market data.

## Risk and alpha

Treat risk and alpha as separate, composable layers. Risk should describe exposures and sensitivities across issuer, sector, rating, curve, spread, liquidity, and portfolio concentration, with clear aggregation and scenario assumptions. Alpha should capture horizon-specific expected excess return, its evidence and uncertainty, and the costs or constraints that may prevent realization. Portfolio construction can combine these layers only through explicit, reviewable assumptions.

## Observatory ethos

Prefer transparent, reproducible research over opaque precision. Make data lineage, model versions, assumptions, uncertainty, and limitations visible. Keep research artifacts modular and inspectable; make it easy to compare an estimate with its source observations and to explain how a view affects a portfolio. The Observatory is an internal decision-support tool, not an execution system or investment recommendation.

## Initial milestones

1. Inventory repository and runtime needs; define naming, units, and core security/issuer identifiers.
2. Audit candidate OpenBB and FINRA coverage and document access, licensing, and gaps.
3. Add provider interfaces, data validation, lineage metadata, and synthetic fixtures.
4. Build a minimal instrument and issuer model plus exploratory risk and alpha components.
5. Add reproducible research examples and a simple Observatory view of inputs, estimates, and portfolio exposures.

## Suggested repository shape

```text
src/parallax/
  data/          # provider interfaces, source adapters, normalization
  domain/        # issuer, instrument, identifiers, shared types
  research/      # valuation, expected trading level, return forecasts
  risk/          # exposures, sensitivities, scenarios
  portfolio/     # portfolio representations and analysis
  observatory/   # research views and reporting
tests/           # unit and integration tests; synthetic fixtures
docs/            # data coverage, assumptions, architecture, decisions
```

Keep the initial structure lightweight and let working code justify new packages.

## Data and compliance

Before using a source, confirm its terms permit the intended internal use, storage, derived data, and redistribution. Respect provider rate limits and attribution requirements. Do not commit credentials, licensed raw data, or sensitive portfolio information. Keep secrets in approved local configuration and document provenance and retention expectations. Synthetic examples must be conspicuously labelled.
# Changelog

All notable changes to this project are recorded in this file. The format is based on Keep a Changelog, and the project aims to follow semantic versioning.

## [Unreleased]

### Added

- KPI Monitor tab on the company detail page: upload a Bloomberg model, earnings transcripts and other documents, and get an LLM review of which metrics changed significantly or look concerning, linked to management commentary. New `kpi_monitor` package, `configs/kpi_monitor_content.yaml`, and an `equities.kpi_monitor` config block.
- KPI reviews are saved per company to the Data Lake (`LLM/kpi-monitor/`) and can be reopened from the tab, including the figures, document passages, focus note and added files.
- Word (.docx) export of a KPI review: title block, summary, at-a-glance table, one section per finding with a trend chart and the underlying figures, sources, and branded header and footer.

### Fixed

- `search_model.retrieve_financial_model_bulk` filtered the index's `companies` field (company names) by ticker, which never matches a model tagged with its ticker in `tickers`. It now matches `tickers`, and also `companies` by ticker or company name. This is the lookup behind the KPI Monitor, the DCF Model Builder and the company Financials tab.
- KPI Monitor: **Run KPI review** now uploads and ingests any files still selected in the uploader first, instead of ignoring them, and says when the index has not caught up yet.

### Changed

- `dcf_parsing_model.ParsedRecord` now carries the source `document_date`.
- KPI metric values are shown without decimals when whole, and with a consistent number of decimals within each metric.

## [0.0.1] - 2026-08-07

### Added

- Documentation set under `docs/` covering the overview, getting started, architecture, configuration, frontend pages, retrieval and chat, the vectorization pipeline, deployment, and development.
- History-aware chat sessions persisted per tab to the Data Lake under `LLM/sessions`, with a retention limit on the most recent sessions.
- Azure DevOps release pipeline definitions under `deploy/`.

### Changed

- Renamed the application package from `avb_llm_frontend` to `equities_frontend`.
- Split the former monolithic `database/` folder into topic packages: `companies`, `dcf`, `index`, `investment_case`, and `upload`, each following the Model View Controller layout.
- Moved the ingestion package formerly at `equities_search/` into `equities_frontend/vectorization/`.
- Consolidated all configuration into `equities_frontend/configs/`.

### Removed

- Docker Compose based local orchestration in favour of a single Docker image and the Azure DevOps pipeline.
- Unused SQLite conversation persistence from the local cache.

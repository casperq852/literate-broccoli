# Changelog

All notable changes to this project are recorded in this file. The format is based on Keep a Changelog, and the project aims to follow semantic versioning.

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

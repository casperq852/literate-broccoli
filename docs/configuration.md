# Configuration

All configuration lives under `equities_frontend/configs/` as checked-in YAML. Endpoints are not treated as secrets and are committed; keys and identities are supplied through environment variables (see [getting-started](getting-started)). This page describes each configuration file and the settings that control behaviour.

## Configuration files

| File | Loaded by | Purpose |
|------|-----------|---------|
| `configs/app_config.yaml` | `app.py`, `scheduler/scheduler_model.py` | Environment blocks and the `equities` application settings for the frontend and scheduler. |
| `configs/config.yaml` | frontend and pipeline | Ingestion parameters, enrichment settings, model extraction, and the search index schema. |
| `configs/auth_config.yaml` | `auth/` | User records for the login gate. Absent in developer mode. |
| `configs/quick_questions.yaml` | chat page | Predefined prompt buttons for the chat page. |
| `dcf/dcf_content.yaml` | DCF builder | Static text and labels for the DCF page. |
| `investment_case/investment_case_content.yaml` | case builder | Section prompts, personas, and dialogue text. |
| `portfolio_dashboard/app_config.yaml` | vendored dashboard | The colleague's own config, resolved relative to their package and left unmodified — separate from this app's `app_config.yaml` above. |

## Environments

`app_config.yaml` defines three environment blocks and top-level `subscription_id` and `client_id` values.

| Environment | Data Lake account |
|-------------|-------------------|
| `development` | `weavbaostoredls01` |
| `acceptance` | `weavbaastoredls01` |
| `production` | `weavbapstoredls01` |

Each block carries `datalake_name`, `resource_group_name`, and `workspace_name`. The active environment is fixed in code by the `DEPLOYMENT_ENVIRONMENT` constant in `deployment_env.py` (read by both `version_selector.py` and the standalone `scheduler/scheduler_model.py`). At startup `app.py` copies the block named by the active environment into `app_config["deployment"]`, so downstream code reads one consistent `deployment` block regardless of environment.

## The equities block

The `equities` block in `app_config.yaml` holds the frontend application settings.

| Key | Default | Meaning |
|-----|---------|---------|
| `data_layer` | `silver` | Data Lake medallion layer the app reads documents from. |
| `index_path` | `LLM/indices/equities-research` | Data Lake path that mirrors the search index. |
| `cache_backup_path` | `SR/LLM/equities-cache/documents_backup.db` | Data Lake path for the scheduler's hourly SQLite cache backup. |
| `index_name` | `equities-research` | Azure AI Search index name. |
| `config_file` | `equities_frontend/configs/config.yaml` | Path to the pipeline and schema config. |
| `allowed_extensions` | `.pdf .xlsx .docx .pptx .md .txt` | File types accepted by the upload pages. |
| `chat_deployment` | `gpt-5.6-sol` | Azure AI Foundry chat deployment for answering. |
| `embedding_deployment` | `text-embedding-3-large` | Embedding deployment for query vectors. |
| `top_k` | `20` | Number of chunks retrieved per query. |
| `max_answer_tokens` | `12000` | Upper bound on the generated answer length. |
| `foundry_endpoint` | set | Azure AI Foundry endpoint. |
| `foundry_api_version` | `2025-03-01-preview` | Foundry API version. |
| `foundry_embedding_deployment` | `text-embedding-3-large` | Embedding deployment name, injected into the environment for `avb_search.schema.SearchIndexSchema`. |
| `search_endpoint` | set | Azure AI Search endpoint. |
| `datalake_account_name` | set | Default Data Lake account, overridden by the `ACCOUNT_NAME` env var when set. |

The block also contains a `company_classification` sub-block (`allowed_regions`, `allowed_gics_sectors`, `batch_size` `40`) that constrains the region/GICS classification prompt the scheduler runs on newly-seen companies, and a `dcf` sub-block with the model defaults (`default_discount_rate` `0.09`, `default_terminal_growth_rate` `0.025`, `default_projection_years` `5`, `default_tax_rate` `0.21`), the allowed input ranges, `max_bulk_records` `2000`, and `research_top_k` `15`.

## Ingestion and enrichment settings

`config.yaml` drives the vectorization pipeline. The main sub-blocks are:

- `ingest`: chunking (`chunk_size` `1200`, `chunk_overlap` `200`, `batch_size` `16`, `include_patterns`).
- `logging`: the pipeline log `level` (`DEBUG`).
- `enrichment`: the metadata-extraction feature flag and its tuning — `enabled`, `model` (the chat deployment used for enrichment), `instructions` (the base prompt prepended to the auto-generated field list), and `sample_chars` (how much of each document's text is sent to the model). There is no fixed enrichment schema here: the avb_search `MetadataEnricher` populates every field in `search_index_configuration.fields` marked `llm_defined: true`, using that field's own `description` to build the structured-output schema — see [vectorization-pipeline](vectorization-pipeline).
- `financial_model_extraction`: `enabled`, `filename_prefix` (`MODL_`), and `data_start_col`, controlling how Bloomberg model exports are recognised and parsed.
- `storage_accounts`: maps `development`, `acceptance`, and `production` to Data Lake account names for the pipeline `--storage_account` argument.
- `search_index_configuration`: the authoritative index schema (see [vectorization-pipeline](vectorization-pipeline)).

Company-name canonicalization (fuzzy-matching a document's extracted company names against tickers already known to the app) is not a `config.yaml` setting — it's a fixed constant (`DEFAULT_FUZZY_THRESHOLD = 80`) in `vectorization/company_resolution.py`, and it only runs in the frontend's own upload path, not the standalone CLI pipeline. See [vectorization-pipeline](vectorization-pipeline).

## Feature flags

Two flags in `config.yaml` switch major pipeline behaviour on or off:

- `enrichment.enabled` toggles LLM metadata enrichment during ingestion. When off, documents are indexed without the `llm_defined` fields (companies, tickers, date, publisher, document type) populated.
- `financial_model_extraction.enabled` toggles Bloomberg model parsing. When off, files with the `MODL_` prefix are treated as ordinary documents.

A third flag lives outside `config.yaml`: the `EQUITIES_SCHEDULER_ENABLED` environment variable (default `true`) disables the hourly background scheduler thread when set to `false`. See [getting-started](getting-started).

## Authentication

The login gate activates only when `configs/auth_config.yaml` exists. In that mode `run_auth()` renders a login form and sets `is_admin` in session state for admin users, which reveals the User Management page. When the file is absent the app runs in developer mode with the gate disabled. Users are added with the `create_user.py` CLI or through the User Management page; passwords are hashed with `bcrypt`.

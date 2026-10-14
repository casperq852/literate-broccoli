# Getting started

This page covers installing the project, providing credentials, and running both the web app and the ingestion pipeline on a local machine.

## Prerequisites

- Python `3.10` (the project pins `>=3.10, <3.11` in `pyproject.toml`).
- Access to the a.s.r. Azure Artifacts feeds, because the internal AVB AI Toolkit packages (`avb-llm` and `avb-search`) are installed from there. Builds and installs must run from within the a.s.r. network.
- Read access to the Azure Data Lake storage accounts and to the Azure AI Search service for the environment you intend to use.
- `uv` is recommended for dependency management, but plain `pip` also works.

## Install

The project uses `uv` with the internal indexes declared in `pyproject.toml`.

```bash
uv sync
```

To install with `pip` instead, point pip at the internal feed and install the project in editable mode:

```bash
pip install -e .
```

## Environment variables

Runtime credentials and connection details are read from the process environment. `version_selector.py` and `main.py` both call `load_dotenv()`, so a local `.env` file at the repository root is loaded automatically. The `.env` file is never committed.

| Variable | Purpose | Default |
|----------|---------|---------|
| `ACCOUNT_NAME` | Overrides the Data Lake storage account name. When unset, the account is taken from `app_config.yaml` for the active environment. | unset |
| `DEFAULT_IDENTITY_CLIENT_ID` | Managed identity client id used to authenticate to the Data Lake. | unset |
| `ACCOUNT_KEY` | Optional Data Lake account key, used by the ingestion pipeline when identity auth is not available. | unset |
| `AZURE_FOUNDRY_API_KEY` | Azure AI Foundry (Azure OpenAI) API key for the chat and embedding clients. | empty |
| `AZURE_FOUNDRY_EMBEDDING_DEPLOYMENT` | Embedding deployment name used during ingestion. | empty |
| `AZURE_SEARCH_API_KEY` | Azure AI Search admin key for reading and writing the index. | empty |
| `APP_PORT` | Port the Streamlit app listens on. | `8501` |
| `LOG_LEVEL` | Logging level for the launcher and pipeline. | `debug` |
| `EQUITIES_SCHEDULER_ENABLED` | Set to `false` to disable the hourly background scheduler thread `main.py` otherwise starts alongside Streamlit. | `true` |

The Azure AI Foundry and Azure AI Search endpoints themselves are not secrets and are checked in under `equities_frontend/configs/config.yaml` and `app_config.yaml`. See [configuration](configuration) for details.

## Run the web app

Start the Streamlit frontend through its launcher, which runs `version_selector.py` under `streamlit run` on the configured port.

```bash
python equities_frontend/main.py --port 8501
```

The app opens at `http://localhost:8501`. When no `auth_config.yaml` exists the app runs in developer mode with the login gate disabled. See [configuration](configuration) for how to enable authentication.

## Run the ingestion pipeline

The pipeline is a console entry point that reads documents from the Data Lake and writes to Azure AI Search. A minimal run against the development storage account looks like this:

```bash
PYTHONPATH=equities_frontend python -m vectorization \
  --index_name equities-research \
  --index_path LLM/indices/equities-research \
  --data_layer silver \
  --config_file equities_frontend/configs/config.yaml \
  --storage_account development \
  --overwrite true
```

`--overwrite true` drops and recreates the index; omit it to add to an existing index. The full set of arguments and the ingestion behaviour are described in [vectorization-pipeline](vectorization-pipeline).

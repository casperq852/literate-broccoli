# Equities Research RAG

Equities Research RAG is an internal a.s.r. Vermogensbeheer application for the Equities desk. It indexes broker research and Bloomberg financial models into Azure AI Search and serves grounded, source-cited answers through a Streamlit web app. It is built on the AVB AI Toolkit, the internal `avb_llm` and `avb_search` packages.

The repository contains two parts of one package:

- `equities_frontend/` is the Streamlit web app: history-aware chat, an investment case builder, a DCF model builder, an index browser, and document upload.
- `equities_frontend/vectorization/` is the ingestion pipeline that enriches, chunks, embeds, and uploads documents to Azure AI Search.

## Quick start

Requires Python `3.10` and access to the a.s.r. Azure Artifacts feeds (for the AVB AI Toolkit packages) from within the a.s.r. network.

```bash
uv sync
```

Provide credentials in a `.env` file at the repository root, then run the app:

```bash
python equities_frontend/main.py --port 8501
```

The app opens at `http://localhost:8501`. Run the ingestion pipeline separately:

```bash
PYTHONPATH=equities_frontend python -m vectorization \
  --index_name equities-research \
  --index_path LLM/indices/equities-research \
  --data_layer silver \
  --config_file equities_frontend/configs/config.yaml \
  --storage_account development \
  --overwrite false
```

## Documentation

Full documentation lives in [docs/](docs/home.md):

- [Getting started](docs/getting-started.md): prerequisites, install, environment variables, and running locally.
- [Architecture](docs/architecture.md): repository layout, the Model View Controller structure, and startup flow.
- [Configuration](docs/configuration.md): the config files, environments, and feature flags.
- [Frontend pages](docs/frontend-pages.md): a tour of each Streamlit page.
- [Retrieval and chat](docs/retrieval-and-chat.md): the retrieval-augmented generation flow and chat sessions.
- [Vectorization pipeline](docs/vectorization-pipeline.md): ingestion, enrichment, model extraction, and the index schema.
- [Deployment](docs/deployment.md): the Docker image and the Azure DevOps pipeline.
- [Development](docs/development.md): coding standards and tooling.

Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

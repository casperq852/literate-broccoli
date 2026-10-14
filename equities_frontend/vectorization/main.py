"""
Command-line entry point for the equities ingestion pipeline.

Wires the avb_search clients, schema, and document processor (all used
unchanged) into the equities extensions ``EquitiesExtractor`` and the
avb_search ``IndexPipeline``. Configuration comes from ``config.yaml`` and
credentials from the environment.

Example::

    python -m vectorization \\
        --index_name equities-research \\
        --index_path LLM/indices/Equities \\
        --data_layer silver \\
        --config_file equities_frontend/configs/config.yaml \\
        --storage_account development \\
        --overwrite true
"""

from __future__ import annotations

import asyncio
import os
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from avb_core import get_logger
from avb_core.files import ConfigFacade
from avb_core.loggers._base import Logger
from avb_core.storage.aio import AsyncDataLakeManager
from avb_search.clients import (
    AzureAIFoundryClient,
    AzureAISearchClient,
    AzureAISearchIndexManager,
)
from avb_search.documents import LangChainDocumentProcessor
from avb_search.enrichment.metadata_controller import MetadataEnricher
from avb_search.pipelines import IndexPipeline
from avb_search.schema import SearchIndexSchema
from vectorization.extraction.extraction_controller import EquitiesExtractor

logger: Logger = get_logger(name=__name__, structured=True)

# Storage account per environment, used only when config.yaml has no
# storage_accounts block and the CLI caller has not set ACCOUNT_NAME.
_FALLBACK_STORAGE_ACCOUNTS = {
    "development": "weavbaostoredls01",
    "acceptance": "weavbaastoredls01",
    "production": "weavbapstoredls01",
}

# Same hardcoded path app.py, scheduler/scheduler_model.py, and helpers.py all
# use. search_endpoint/foundry_endpoint/foundry_api_version live here (under
# the flat 'equities' block, no nesting) -- config.yaml (--config_file) has no
# such keys, despite FoundrySettings/SearchSettings historically expecting an
# 'azure' block on it; that always silently resolved to an empty endpoint.
_APP_CONFIG_PATH = Path("equities_frontend/configs/app_config.yaml")


def _load_equities_app_config() -> dict[str, Any]:
    """Load the 'equities' block of app_config.yaml (endpoints, deployment names)."""
    with open(_APP_CONFIG_PATH) as config_file:
        app_config: dict[str, Any] = yaml.safe_load(config_file)
    return app_config["equities"]


@dataclass(frozen=True)
class FoundrySettings:
    """Azure AI Foundry connection settings."""

    endpoint: str
    api_version: str
    embedding_deployment: str

    @classmethod
    def from_config(cls, equities_app_config: dict[str, Any]) -> FoundrySettings:
        return cls(
            endpoint=equities_app_config["foundry_endpoint"],
            api_version=equities_app_config["foundry_api_version"],
            embedding_deployment=equities_app_config["embedding_deployment"],
        )


@dataclass(frozen=True)
class SearchSettings:
    """Azure AI Search connection settings."""

    endpoint: str

    @classmethod
    def from_config(cls, equities_app_config: dict[str, Any]) -> SearchSettings:
        return cls(endpoint=equities_app_config["search_endpoint"])


def _parse_args() -> Namespace:
    parser = ArgumentParser(description="Equities research ingestion pipeline")
    parser.add_argument("--index_name", required=True, help="Azure AI Search index name")
    parser.add_argument("--index_path", required=True, help="Data Lake path to index")
    parser.add_argument("--data_layer", required=True, help="Data Lake filesystem (e.g. silver)")
    parser.add_argument("--config_file", required=True, help="Path to configs/config.yaml")
    parser.add_argument("--pdf_parsing_method", choices=["pages", "full-content"], default="pages")
    parser.add_argument(
        "--overwrite",
        choices=["true", "false"],
        default="false",
        help="Delete and recreate the whole search index before running (e.g. after a schema change).",
    )
    parser.add_argument(
        "--reindex_existing",
        choices=["true", "false"],
        default="false",
        help="Re-process a file even if it is already indexed, replacing its existing chunks. "
        "When false, already-indexed files are skipped.",
    )
    parser.add_argument(
        "--storage_account",
        choices=list(_FALLBACK_STORAGE_ACCOUNTS),
        default="development",
    )
    return parser.parse_args()


def _build_enricher(config: ConfigFacade, foundry_client: AzureAIFoundryClient) -> MetadataEnricher | None:
    """Build the avb_search ``MetadataEnricher`` if enrichment is enabled in config, else ``None``."""
    enrichment = config.get("enrichment", {})
    if not enrichment.get("enabled", False):
        logger.info(message="Metadata enrichment disabled (enrichment.enabled=false)")
        return None

    logger.info(message="Metadata enrichment enabled")
    return MetadataEnricher(config=config, foundry_client=foundry_client)


def _build_extractor(config: ConfigFacade, *, per_page_pdf: bool) -> EquitiesExtractor:
    """Build an ``EquitiesExtractor`` honouring the financial-model feature flag."""
    model_config = config.get("financial_model_extraction", {})
    prefix = model_config.get("filename_prefix") if model_config.get("enabled", False) else None
    if prefix:
        logger.info(message=f"Financial model extraction enabled (prefix={prefix})")
    extractor_kwargs: dict[str, Any] = {}
    if "data_start_col" in model_config:
        extractor_kwargs["data_start_col"] = model_config["data_start_col"]
    return EquitiesExtractor(per_page_pdf=per_page_pdf, financial_model_prefix=prefix, **extractor_kwargs)


def _build_pipeline(args: Namespace, config: ConfigFacade, equities_app_config: dict[str, Any]) -> IndexPipeline:
    """Assemble the pipeline from config, env settings, and CLI arguments."""
    foundry = FoundrySettings.from_config(equities_app_config)
    search = SearchSettings.from_config(equities_app_config)
    ingest = config["ingest"]
    index_config = config["search_index_configuration"]
    storage_accounts = config.get("storage_accounts", _FALLBACK_STORAGE_ACCOUNTS)
    account_name = os.getenv("ACCOUNT_NAME") or storage_accounts[args.storage_account]

    foundry_client = AzureAIFoundryClient(
        endpoint=foundry.endpoint,
        api_version=foundry.api_version,
        embedding_deployment=foundry.embedding_deployment,
    )

    return IndexPipeline(
        index_name=args.index_name,
        index_path=args.index_path,
        data_layer=args.data_layer,
        manager=AsyncDataLakeManager(
            account_name=account_name,
            account_key=os.getenv("ACCOUNT_KEY"),
            client_id=os.getenv("DEFAULT_IDENTITY_CLIENT_ID"),
        ),
        extractor=_build_extractor(config=config, per_page_pdf=args.pdf_parsing_method == "pages"),
        document_processor=LangChainDocumentProcessor(
            chunk_size=ingest["chunk_size"],
            chunk_overlap=ingest["chunk_overlap"],
        ),
        search_index=AzureAISearchIndexManager(
            endpoint=search.endpoint,
            index_name=args.index_name,
            recreate_index=args.overwrite == "true",
        ),
        search_client=AzureAISearchClient(
            endpoint=search.endpoint,
            index_name=args.index_name,
        ),
        search_index_schema=SearchIndexSchema(
            index_name=args.index_name,
            fields_configuration=index_config["fields"],
            vector_search_configuration=index_config["vector_search"],
            azure_openai_endpoint=foundry.endpoint,
            azure_openai_embedding_deployment=foundry.embedding_deployment,
        ),
        foundry_client=foundry_client,
        metadata_enricher=_build_enricher(config=config, foundry_client=foundry_client),
        overwrite=args.reindex_existing == "true",
    )


async def run_pipeline() -> None:
    """Load environment, build the pipeline, and run it."""
    load_dotenv(override=True)

    args = _parse_args()
    config = ConfigFacade(config_path=args.config_file).config
    equities_app_config = _load_equities_app_config()
    os.environ["LOG_LEVEL"] = config.get("logging", {}).get("level", "DEBUG")

    logger.info(message="Initializing IndexPipeline")
    pipeline = _build_pipeline(args=args, config=config, equities_app_config=equities_app_config)
    await pipeline.run()


def run_pipeline_sync() -> None:
    """Synchronous wrapper used as the console-script entry point."""
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    run_pipeline_sync()

"""
Ingestion model for the upload topic.

Builds an ``IndexPipeline`` wired to the selected deployment and runs ad-hoc
ingestion of already-uploaded Data Lake paths into the existing search index,
then refreshes the local document and company caches. This is the heavy
ingestion machinery for the upload topic; ``UploadController`` stays thin and
simply delegates here.

Everything here talks only to Azure services (Data Lake, AI Search, Foundry),
the only outbound access permitted in the a.s.r. environment.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from avb_core.files import ConfigFacade
from avb_core.storage.aio import AsyncDataLakeManager
from avb_search.clients import (
    AzureAIFoundryClient,
    AzureAISearchClient,
    AzureAISearchIndexManager,
)
from avb_search.documents import LangChainDocumentProcessor
from avb_search.enrichment.metadata_controller import MetadataEnricher
from avb_search.pipelines import IndexPipeline
from avb_search.pipelines.index_pipeline_model import build_path_filter
from avb_search.schema import SearchIndexSchema
from companies.company_controller import CompanyController
from database.cache import get_all_companies, get_documents_by_paths, post_ingest_update
from helpers import CLIENT_ID, datalake_account, equities_config
from search.search_model import get_search_client
from vectorization.company_resolution import resolve_companies
from vectorization.extraction.extraction_controller import EquitiesExtractor

logger = logging.getLogger(__name__)

# Max files processed concurrently during an ad-hoc upload ingest.
DEFAULT_FILE_CONCURRENCY = 4


def ingest_paths(
    file_paths: list[str],
    on_progress: Callable[[str, int, int], None] | None = None,
    update_cache: bool = True,
    overwrite: bool = False,
) -> dict[str, int]:
    """
    Ingest one or more Data Lake paths into the existing search index.

    Runs each path through the avb_search ``IndexPipeline`` under a shared manager,
    up to ``DEFAULT_FILE_CONCURRENCY`` files concurrently. Never recreates the
    index -- safe to run while others query or upload.

    Args:
        file_paths (list[str]): Data Lake paths (relative to the data_layer filesystem).
        on_progress (Callable[[str, int, int], None] | None): Optional callback fired
            on the main thread each time a file completes. Receives
            (file_path, completed_count, total_count).
        update_cache (bool): Whether to refresh the local document and company
            caches after a successful ingest.
        overwrite (bool): When a path is already indexed, whether to delete its
            existing chunks before re-ingesting. Without this, re-ingesting a file
            whose new content has fewer chunks than before leaves the extra old
            chunks behind (they only get overwritten where a new chunk lands on
            the same deterministic id, never removed). Defaults to False, which
            matches the previous behaviour of always re-ingesting without
            cleaning up first.

    Returns:
        dict[str, int]: Mapping of file_path -> number of segments ingested. A value
        of 0 means nothing was extracted (e.g. an unsupported or empty file).
    """
    results = asyncio.run(_ingest_paths_async(file_paths, on_progress, overwrite=overwrite))
    if update_cache:
        successful = [p for p, segs in results.items() if segs > 0]
        try:
            if successful:
                post_ingest_update(successful, get_search_client(equities_config()["index_name"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not update local document cache after ingest: %s", exc)
            successful = []

        if successful:
            try:
                cfg = equities_config()
                tickers = {
                    ticker
                    for doc in get_documents_by_paths(successful)
                    for ticker in (doc.get("companies") or [])
                    if ticker
                }
                if tickers:
                    CompanyController(cfg["index_name"]).sync_companies_incremental(list(tickers), cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not refresh the local company cache after ingest: %s", exc)
    return results


async def _ingest_paths_async(
    file_paths: list[str],
    on_progress: Callable[[str, int, int], None] | None = None,
    overwrite: bool = False,
) -> dict[str, int]:
    logger.debug("building pipeline for %d file(s)", len(file_paths))
    t0 = time.perf_counter()
    pipeline = build_pipeline()
    await pipeline.manager.async_init()
    # Fetched once per batch, not per file -- every concurrent _ingest_one() call
    # shares (and mutates, see _ingest_one) this same dict of already-known
    # companies, so a company discovered by one file in the batch is visible
    # to every other file in the same batch too, not just to later batches.
    known_companies = {
        company["ticker"]: company["display_name"]
        for company in get_all_companies()
        if company.get("ticker") and company.get("display_name")
    }
    logger.debug("pipeline ready (%.2fs)", time.perf_counter() - t0)
    try:
        sem = asyncio.Semaphore(DEFAULT_FILE_CONCURRENCY)

        async def _process_one(path: str) -> tuple[str, int]:
            async with sem:
                logger.debug("starting %s", path)
                t1 = time.perf_counter()
                try:
                    segs = await _ingest_one(pipeline, path, known_companies, overwrite=overwrite)
                    logger.debug("finished %s — %d segments (%.2fs)", path, segs, time.perf_counter() - t1)
                    return path, segs
                except Exception as exc:  # noqa: BLE001
                    logger.error("failed %s after %.2fs — %s", path, time.perf_counter() - t1, exc)
                    return path, 0

        total = len(file_paths)
        results: dict[str, int] = {}
        tasks = [asyncio.create_task(_process_one(p)) for p in file_paths]
        for coro in asyncio.as_completed(tasks):
            path, segs = await coro
            results[path] = segs
            if on_progress:
                on_progress(path, len(results), total)
        logger.debug("all %d file(s) done (%.2fs total)", total, time.perf_counter() - t0)
        return results
    finally:
        await pipeline.manager.close()


async def _ingest_one(
    pipeline: IndexPipeline, path: str, known_companies: dict[str, str], overwrite: bool = False
) -> int:
    """
    Ingest a single Data Lake path into the existing index.

    Replicates the per-file flow (download -> extract -> enrich -> resolve ->
    chunk -> upload) using only the public ``IndexPipeline`` methods, so uploads
    go through ``upload_langchain_documents`` -- which coerces date fields to a
    valid ``Edm.DateTimeOffset`` and defaults missing collection fields to an
    empty list, instead of failing the whole batch.

    When ``overwrite`` is set, any chunks already indexed for this exact path are
    deleted first (via an OData filter on the ``path`` field), so a re-ingested
    file whose content shrank does not leave orphaned chunks from the previous
    version behind -- new chunks only overwrite old ones where their
    deterministic id happens to collide, never where it doesn't.

    The resolve step (``vectorization.company_resolution.resolve_companies``)
    runs between enrichment and chunking so canonicalized company names/tickers
    go straight into the uploaded document -- never briefly live in the index
    unresolved, and no separate patch/merge step needed afterwards.

    Args:
        pipeline (IndexPipeline): An initialized pipeline (its manager must already
            have had ``async_init`` called).
        path (str): Data Lake path to ingest, relative to the pipeline's
            ``data_layer`` filesystem.
        known_companies (dict[str, str]): Ticker -> canonical display name, from
            ``database.cache.get_all_companies()``, used to canonicalize this
            file's enriched company mentions. Shared by reference across every
            concurrent ``_ingest_one`` call in the same batch (see
            ``_ingest_paths_async``) and mutated in place below as this file
            resolves companies, so a brand-new company mentioned by two files
            in the same batch (e.g. under two different exchange tickers)
            still gets to canonicalize onto one entry instead of each file
            independently deciding it's new -- resolve_companies() has
            nothing to match a second ticker against otherwise, since the
            first file's resolution wouldn't be in the local SQLite cache
            yet (that only happens after the whole batch commits).
        overwrite (bool): Delete this path's existing indexed chunks before
            re-ingesting. Defaults to False.

    Returns:
        int: Number of segments extracted (0 if the file yielded nothing).
    """
    if overwrite:
        deleted = await asyncio.to_thread(pipeline.search_client.delete_documents_by_filter, build_path_filter(path=path))
        if deleted:
            logger.info("Overwriting %s: deleted %d existing chunk(s) before re-indexing.", path, deleted)

    file_bytes = await pipeline.manager.download_file(filesystem_name=pipeline.data_layer, file_path=path)
    folder_metadata = pipeline.extract_folder_metadata(file_path=path)

    segments = await asyncio.to_thread(pipeline.generate_extract_segments_from_path, Path(path), file_bytes)
    if not segments:
        return 0

    segments = await pipeline.enrich_extract_segments(
        segments=segments, filename=Path(path).name, folder_metadata=folder_metadata
    )
    # No `await` from here through the loop below, so this whole stretch runs
    # as one uninterrupted turn on the event loop -- the known_companies
    # mutation is therefore atomic with respect to every other concurrently
    # scheduled _ingest_one() call, not just "usually fine."
    for segment in segments:
        resolved_names, resolved_tickers = resolve_companies(
            raw_names=segment.meta.get("companies") or [],
            raw_tickers=segment.meta.get("tickers") or [],
            known_companies=known_companies,
        )
        segment.meta["companies"] = resolved_names
        segment.meta["tickers"] = resolved_tickers
        for ticker, name in zip(resolved_tickers, resolved_names, strict=False):
            if ticker and name:
                known_companies.setdefault(ticker, name)

    documents = pipeline.split_extract_segments_into_langchain_documents(segments=segments)
    await asyncio.to_thread(pipeline.upload_langchain_documents, documents=documents)
    return len(segments)


def build_pipeline() -> IndexPipeline:
    """
    Construct an ``IndexPipeline`` wired to the currently selected deployment.

    Uses the custom ``EquitiesExtractor`` and the field-driven avb_search
    ``MetadataEnricher``, which enriches every ``search_index_configuration``
    field marked ``llm_defined: true``. Foundry endpoint/credential resolution is
    handled inside the ``avb_search`` clients.

    Returns:
        IndexPipeline: A ready-to-run pipeline (its Data Lake manager still
        needs ``async_init`` before use).
    """
    t0 = time.perf_counter()
    cfg: Any = equities_config()
    account_name = datalake_account()
    pipeline_config = ConfigFacade(config_path=cfg["config_file"]).config

    search_endpoint = cfg["search_endpoint"]

    foundry_client = AzureAIFoundryClient(
        endpoint=cfg["foundry_endpoint"],
        api_version=cfg["foundry_api_version"],
        embedding_deployment=cfg["embedding_deployment"],
    )

    enrichment = pipeline_config.get("enrichment", {})
    enricher = (
        MetadataEnricher(config=pipeline_config, foundry_client=foundry_client)
        if enrichment.get("enabled", False)
        else None
    )

    model_cfg = pipeline_config.get("financial_model_extraction", {})
    extractor_kwargs = {}
    if "data_start_col" in model_cfg:
        extractor_kwargs["data_start_col"] = model_cfg["data_start_col"]
    extractor = EquitiesExtractor(
        per_page_pdf=True,
        financial_model_prefix=(model_cfg.get("filename_prefix") if model_cfg.get("enabled", False) else None),
        **extractor_kwargs,
    )

    index_config = pipeline_config["search_index_configuration"]
    logger.debug("constructing pipeline (%.2fs)", time.perf_counter() - t0)
    return IndexPipeline(
        metadata_enricher=enricher,
        index_name=cfg["index_name"],
        index_path=cfg["index_path"],
        data_layer=cfg["data_layer"],
        manager=AsyncDataLakeManager(account_name=account_name, client_id=CLIENT_ID),
        extractor=extractor,
        document_processor=LangChainDocumentProcessor(
            chunk_size=pipeline_config["ingest"]["chunk_size"],
            chunk_overlap=pipeline_config["ingest"]["chunk_overlap"],
        ),
        search_index=AzureAISearchIndexManager(
            endpoint=search_endpoint,
            index_name=cfg["index_name"],
            recreate_index=False,  # never recreate the index from the frontend
        ),
        search_client=AzureAISearchClient(
            endpoint=search_endpoint,
            index_name=cfg["index_name"],
        ),
        search_index_schema=SearchIndexSchema(
            index_name=cfg["index_name"],
            fields_configuration=index_config["fields"],
            vector_search_configuration=index_config["vector_search"],
            azure_openai_endpoint=cfg["foundry_endpoint"],
            azure_openai_embedding_deployment=cfg["foundry_embedding_deployment"],
        ),
        foundry_client=foundry_client,
    )

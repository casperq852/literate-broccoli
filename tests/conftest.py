"""
Shared test setup.

Stubs the org-internal ``avb_core``/``avb_llm``/``avb_search`` SDKs (only
installable from the a.s.r. Azure Artifacts feed) so unit tests can import
modules like ``search.search_model`` and ``vectorization.company_resolution``
and exercise their pure functions without network access or feed credentials.
Only the names actually referenced at import time by the modules under test
are stubbed -- nothing behavioural is faked here, since none of the pure
functions under test call into these clients.
"""

from __future__ import annotations

import sys
import types


def _stub_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_avb_stubs() -> None:
    if "avb_core" in sys.modules:
        return

    avb_core_storage_aio = _stub_module("avb_core.storage.aio")
    avb_core_storage_aio.AsyncDataLakeManager = type("AsyncDataLakeManager", (), {})

    avb_core_storage_datalakemanager = _stub_module("avb_core.storage.datalakemanager")
    avb_core_storage_datalakemanager.DataLakeManager = type("DataLakeManager", (), {})

    avb_core_storage = _stub_module("avb_core.storage")
    avb_core_storage.aio = avb_core_storage_aio
    avb_core_storage.datalakemanager = avb_core_storage_datalakemanager

    avb_core = _stub_module("avb_core")
    avb_core.storage = avb_core_storage

    avb_llm_clients = _stub_module("avb_llm.clients")
    avb_llm_clients.AzureAIFoundryClient = type("AzureAIFoundryClient", (), {})
    avb_llm_clients.AzureAISearchClient = type("AzureAISearchClient", (), {})

    avb_llm = _stub_module("avb_llm")
    avb_llm.clients = avb_llm_clients

    avb_search_extractor_base = _stub_module("avb_search.extractor._base")
    avb_search_extractor_base.ExtractFigureSegment = type("ExtractFigureSegment", (), {})
    avb_search_extractor_base.ExtractSegment = type("ExtractSegment", (), {})
    avb_search_extractor_base.ExtractTableSegment = type("ExtractTableSegment", (), {})

    avb_search_extractor = _stub_module("avb_search.extractor")
    avb_search_extractor.Extractor = type("Extractor", (), {})
    avb_search_extractor._base = avb_search_extractor_base

    avb_search = _stub_module("avb_search")
    avb_search.extractor = avb_search_extractor


_install_avb_stubs()

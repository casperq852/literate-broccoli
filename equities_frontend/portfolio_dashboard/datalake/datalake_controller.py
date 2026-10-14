"""Thin, config-driven async wrapper around the avb_core Data Lake manager.

The wrapper hides the avb_core :class:`AsyncDataLakeManager` behind a small, typed
surface used by the medallion pipeline: resolving the environment-specific storage
account and managed-identity client id from configuration, listing the landing zone,
and reading/writing bytes for the bronze and silver layers. Concentrating the async
I/O here keeps the orchestration controller free of SDK details (a.s.r. Controller
layer). Callers open the client, run their awaited work, and close it, typically
on one shared background event loop (see :func:`run_async`) at the synchronous
Streamlit boundary.

Each medallion layer may live in a different storage account: the landing zone is
read from the production account while the promoted bronze/silver layers are written
to the development account. Accounts are resolved per layer from configuration, and
one async manager is opened per distinct account.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Coroutine
from io import BytesIO
from types import TracebackType
from typing import Any, TypeVar

from avb_core.storage.aio import AsyncDataLakeManager

from portfolio_dashboard.config import load_app_config

# The Azure SDK HTTP logging policy and the avb_core manager each emit an INFO
# line per request/connection. The dashboard runs the root logger at INFO,
# which would otherwise flood the console with a log for every file, so these
# third-party loggers (and any child loggers they register) are capped at
# WARNING. avb_core raises its own child logger to INFO during async_init, so
# this is also re-applied after the manager is opened.
_NOISY_LOGGER_ROOTS = ("azure", "avb_core")


def _silence_sdk_loggers() -> None:
    """Cap the noisy Azure/avb_core loggers (and their children) at WARNING."""
    names = set(_NOISY_LOGGER_ROOTS)
    names.update(
        name
        for name in logging.root.manager.loggerDict
        if any(name == root or name.startswith(f"{root}.") for root in _NOISY_LOGGER_ROOTS)
    )
    for name in names:
        logging.getLogger(name).setLevel(logging.WARNING)


# aiohttp reports client sessions/connectors that avb_core leaves open when its
# short-lived async manager is garbage-collected. It routes these through the
# asyncio exception handler, which the "asyncio" logger emits at ERROR level as
# "Unclosed client session"/"Unclosed connector". They are harmless for this
# open-run-close usage, so a targeted filter drops just those records without
# hiding genuine asyncio errors (capping the logger would not suppress ERRORs).
_UNCLOSED_MARKERS = ("Unclosed client session", "Unclosed connector")


class _UnclosedAiohttpFilter(logging.Filter):
    """Drop aiohttp's 'Unclosed client session/connector' asyncio errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return ``False`` for the noisy unclosed-session/connector records."""
        message = record.getMessage()
        return not any(marker in message for marker in _UNCLOSED_MARKERS)


def _silence_unclosed_session_errors() -> None:
    """Attach the unclosed-aiohttp filter to the asyncio logger exactly once."""
    asyncio_logger = logging.getLogger("asyncio")
    if not any(isinstance(existing, _UnclosedAiohttpFilter) for existing in asyncio_logger.filters):
        asyncio_logger.addFilter(_UnclosedAiohttpFilter())


_silence_sdk_loggers()
_silence_unclosed_session_errors()


# Streamlit reruns the whole script on every interaction, so a single user action
# chains several synchronous datalake calls. Driving each with ``asyncio.run``
# spins up and tears down a fresh event loop per call; avb_core (aiohttp +
# azure-identity) leaves sessions/connectors bound to the loop they were created
# on, so a later loop touching one raises ``RuntimeError: ... attached to a
# different loop`` / ``Session is closed``. Running every coroutine on one
# process-wide loop (owned by a dedicated daemon thread) keeps every session on a
# single stable loop for the process lifetime, which removes the crash.
_T = TypeVar("_T")


class _BackgroundLoop:
    """A process-wide asyncio loop running on a dedicated daemon thread."""

    def __init__(self) -> None:
        """Start the daemon thread and its never-ending event loop."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="datalake-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Bind the loop to this thread and run it until the process exits."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Submit a coroutine to the background loop and block for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


class _LoopHolder:
    """Lazily-created holder for the process-wide background loop singleton."""

    instance: _BackgroundLoop | None = None


_background_loop_lock = threading.Lock()


def run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run a coroutine on the shared datalake loop, blocking for its result.

    Use this instead of :func:`asyncio.run` for every synchronous entry point that
    drives the async Data Lake client. All avb_core sessions then share one stable
    event loop for the process lifetime, so Streamlit's repeated reruns never reuse
    an aiohttp session across loops (which raises ``attached to a different loop``
    / ``Session is closed``).

    Args:
        coro: The coroutine to run to completion.

    Returns:
        The coroutine's result.
    """
    if _LoopHolder.instance is None:
        with _background_loop_lock:
            if _LoopHolder.instance is None:
                _LoopHolder.instance = _BackgroundLoop()
    return _LoopHolder.instance.run(coro)


class DatalakeClient:
    """Config-driven async facade over the avb_core Data Lake manager.

    Attributes:
        containers: Layer name (``landing``/``bronze``/``silver``) to container map.
        root: Root folder shared by every layer, for example ``EQT``.
    """

    def __init__(self, account_name: str | None = None, client_id: str | None = None) -> None:
        """Resolve the per-layer accounts and identity and prepare lazy managers.

        Args:
            account_name: Explicit storage account; when given, every layer uses
                it (single-account mode). When ``None`` each layer is resolved
                independently from the configured per-layer environments.
            client_id: Explicit managed-identity client id; when ``None`` it is
                read from the configured environment variable.
        """
        config = load_app_config()["datalake"]
        self._config: dict = config
        self._client_id: str | None = client_id or os.getenv(str(config["client_id_env_var"]))
        self.containers: dict[str, str] = dict(config["containers"])
        self.root: str = str(config["root"])
        self.subfolder: str = str(config["bronze_silver_subfolder"])
        self._layer_environments: dict[str, str] = self._resolve_layer_environments(config)
        self._layer_accounts: dict[str, str] = self._resolve_layer_accounts(
            config, self._layer_environments, account_name
        )
        self._managers: dict[str, AsyncDataLakeManager] = {}

    @staticmethod
    def _resolve_layer_environments(config: dict) -> dict[str, str]:
        """Return the environment (development/acceptance/production) per layer.

        Each medallion layer may be pinned to a specific environment via the
        ``layer_environments`` config; layers left unset fall back to the active
        ``default_environment`` (overridable through the environment variable).

        Args:
            config: The ``datalake`` configuration section.

        Returns:
            dict[str, str]: A container-key to environment-name mapping.
        """
        default_environment = os.getenv(str(config["environment_env_var"]), str(config["default_environment"]))
        layer_environments = config.get("layer_environments", {})
        return {
            container: str(layer_environments.get(container, default_environment)) for container in config["containers"]
        }

    @staticmethod
    def _resolve_layer_accounts(
        config: dict, layer_environments: dict[str, str], override: str | None = None
    ) -> dict[str, str]:
        """Return the storage account backing each medallion layer.

        Args:
            config: The ``datalake`` configuration section.
            layer_environments: The resolved environment name per container.
            override: When set, every layer is mapped to this account instead of
                its configured environment (single-account mode).

        Returns:
            dict[str, str]: A container-key to storage-account mapping.

        Raises:
            KeyError: When a resolved environment is not present in ``accounts``.
        """
        accounts = config["accounts"]
        resolved: dict[str, str] = {}
        for container, environment in layer_environments.items():
            if override is not None:
                resolved[container] = override
                continue
            if environment not in accounts:
                raise KeyError(f"Unknown datalake environment '{environment}'; expected one of {sorted(accounts)}.")
            resolved[container] = str(accounts[environment])
        return resolved

    @property
    def account_name(self) -> str:
        """Return the storage account backing the landing zone."""
        return self._layer_accounts["landing"]

    def account_for(self, container: str) -> str:
        """Return the storage account backing a given medallion layer.

        Args:
            container: A layer key from :attr:`containers`.

        Returns:
            str: The resolved storage-account name for that layer.

        Raises:
            KeyError: When the container key is unknown.
        """
        if container not in self._layer_accounts:
            raise KeyError(f"Unknown container '{container}'; expected one of {sorted(self._layer_accounts)}.")
        return self._layer_accounts[container]

    def environment_for(self, container: str) -> str:
        """Return the environment name backing a given medallion layer.

        Args:
            container: A layer key from :attr:`containers`.

        Returns:
            str: The environment name, for example ``development`` or ``production``.

        Raises:
            KeyError: When the container key is unknown.
        """
        if container not in self._layer_environments:
            raise KeyError(f"Unknown container '{container}'; expected one of {sorted(self._layer_environments)}.")
        return self._layer_environments[container]

    async def open(self) -> None:
        """No-op kept for API symmetry; managers are opened lazily per account.

        Connections are established on first use by :meth:`_ensure_manager`, so a
        client that only touches one layer never authenticates against the other
        layers' storage accounts. Use :meth:`close` (or ``async with``) to release
        any managers opened during use.
        """
        return None

    async def _ensure_manager(self, container: str) -> AsyncDataLakeManager:
        """Return the manager for a container's account, opening it on demand.

        Only the storage account backing the requested layer is connected, and the
        connection is reused for every later call within the same session.

        Args:
            container: A layer key from :attr:`containers`.

        Returns:
            AsyncDataLakeManager: The initialised manager for that layer's account.
        """
        account = self.account_for(container)
        manager = self._managers.get(account)
        if manager is not None:
            return manager
        manager = AsyncDataLakeManager(account_name=account, client_id=self._client_id)
        # avb_core emits several INFO lines and attaches its own handler while
        # connecting; mute records at/below INFO for the duration of async_init,
        # then cap the SDK loggers for every later request.
        logging.disable(logging.INFO)
        try:
            await manager.async_init()
        finally:
            logging.disable(logging.NOTSET)
        _silence_sdk_loggers()
        self._managers[account] = manager
        return manager

    async def close(self) -> None:
        """Close every open async Data Lake manager and release its connections."""
        for manager in self._managers.values():
            await manager.close()
        self._managers.clear()

    async def __aenter__(self) -> DatalakeClient:
        """Open the client for use as an async context manager."""
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client when leaving the async context."""
        await self.close()

    def _layer_prefix(self, container: str) -> str:
        """Return the only path prefix this client may touch in a container.

        The landing zone is flat under the root (``EQT/``); the promoted bronze
        and silver layers are confined to the ``EQT/portfolio/`` sub-tree. Every
        read and write is validated against this prefix so the pipeline can never
        reach data outside the equities portfolio feed.

        Args:
            container: A layer key from :attr:`containers`.

        Returns:
            str: The allowed path prefix, ending in a slash.

        Raises:
            KeyError: When the container key is unknown.
        """
        if container not in self.containers:
            raise KeyError(f"Unknown container '{container}'; expected one of {sorted(self.containers)}.")
        if container == "landing":
            return f"{self.root}/"
        return f"{self.root}/{self.subfolder}/"

    def _guard_path(self, container: str, path: str) -> str:
        """Validate that a path stays within the container's allowed prefix.

        Args:
            container: A layer key from :attr:`containers`.
            path: The candidate file path.

        Returns:
            str: The validated path, unchanged.

        Raises:
            ValueError: When the path is absolute, contains a ``..`` segment, or
                falls outside the allowed prefix for the container.
        """
        prefix = self._layer_prefix(container)
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"Refusing unsafe path '{path}'.")
        if not path.startswith(prefix):
            raise ValueError(f"Path '{path}' is outside the allowed '{prefix}' prefix for container '{container}'.")
        return path

    async def list_landing_names(self) -> list[str]:
        """List the full paths directly under the landing-zone root.

        Returns:
            list[str]: Every path name (files and directories) below the root.
        """
        manager = await self._ensure_manager("landing")
        names: list[str] = []
        async for item in manager.list_paths(filesystem_name=self.containers["landing"], path=self.root, recursive=False):
            names.append(item.name)
        return names

    async def list_files(self, container: str) -> list[str]:
        """List every file path (recursively) under the root of a container.

        Args:
            container: A layer key from :attr:`containers`.

        Returns:
            list[str]: File paths below the root, excluding directories.
        """
        manager = await self._ensure_manager(container)
        files: list[str] = []
        list_root = self._layer_prefix(container).rstrip("/")
        async for item in manager.list_paths(filesystem_name=self.containers[container], path=list_root, recursive=True):
            if not item.is_directory:
                files.append(item.name)
        return files

    async def download(self, container: str, path: str) -> bytes:
        """Download a file's bytes from a medallion container.

        Args:
            container: A layer key from :attr:`containers`.
            path: The file path within the container.

        Returns:
            bytes: The file content.
        """
        manager = await self._ensure_manager(container)
        self._guard_path(container, path)
        return await manager.download_file(filesystem_name=self.containers[container], file_path=path)

    async def exists(self, container: str, path: str) -> bool:
        """Report whether a file exists in a medallion container.

        Args:
            container: A layer key from :attr:`containers`.
            path: The file path within the container.

        Returns:
            bool: ``True`` when the file exists.
        """
        manager = await self._ensure_manager(container)
        self._guard_path(container, path)
        return bool(await manager.check_file_exists(filesystem_name=self.containers[container], file_path=path))

    async def upload(self, container: str, path: str, content: bytes) -> None:
        """Upload bytes to a medallion container, overwriting any existing file.

        Args:
            container: A layer key from :attr:`containers`.
            path: The destination file path within the container.
            content: The bytes to write.
        """
        manager = await self._ensure_manager(container)
        self._guard_path(container, path)
        await manager.upload_file(
            filesystem_name=self.containers[container],
            file_path=path,
            file_content=BytesIO(content),
            archive=False,
            overwrite=True,
        )

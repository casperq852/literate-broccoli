"""
Portfolio Dashboard page — hosts the colleague's Bloomberg portfolio analytics
dashboard (vendored, unmodified, under ``portfolio_dashboard/``) as one page of
this app.

``DashboardController.run()`` renders their own sidebar and ``st.tabs(...)``
underneath this app's top nav banner -- that's the whole of the "their
navigation visible underneath the existing banner" requirement, no extra
wiring needed here.

The controller (and its ``SilverStore``) is cached process-wide via
``st.cache_resource``, not per-session: it holds only shared research data,
not per-user state, so every session reuses one instance instead of building
its own. Per-user UI selections (active family/portfolio, tab, sliders) still
live in ``st.session_state`` inside ``DashboardController.run()`` itself, so
they stay independent per session; only the underlying data is shared.

Sharing the store this way has one real consequence, worked around below:
``SilverStore``'s underlying ``DatalakeClient`` opens and closes its own
aiohttp session per call (``async with self.client:`` in
``datalake/silver_controller.py``) -- fine for one caller at a time, but two
Streamlit sessions now share this one store, and two concurrent calls into an
un-cached method can race on that same open/close cycle. That raised
``AssertionError: assert self._connector is not None`` inside aiohttp in
production, from ``render_sidebar_status`` -> ``store.summary_history()``,
which runs on *every* page load, not just when a Trends tab is opened.

Three store methods (``load_reports_by_portfolio``, the "overview" tabs' data
source; ``summary_history``, the sidebar's report-date picker; and
``aum_history_for_model_portfolio``, the Transacties tab's AUM lookup) are
wrapped to prefer the local SQLite cache (silver_cache_model.py) instead of
touching the live client at all under normal operation: SilverStore's own
reads are a recursive walk of the entire silver-layer directory tree, which
grows with every day of new Bloomberg exports and has already timed out in
production under repeated requests. The scheduler now does that walk for real
only once a day (scheduler/scheduler_controller.py::_run_portfolio_sync) and
persists the results to SQLite; this page just reads them back -- which also
happens to make the concurrency race above moot for these paths, since a
SQLite read never touches the shared client at all. ``aum_history_for_model_portfolio``
is derived here from the same cached data ``summary_history`` already
persists (``end_mkt_val``/``as_of``) plus the cached portfolio-name list, so
it needs no SQLite table of its own.

Every other store method the dashboard's tabs call (``has_data``,
``factor_history``, ``sector_history``, ``portfolios``) isn't cached the same
way -- those back specific tabs a session opens on demand, not something
worth a whole SQLite table each for. They're wrapped instead to serialize
behind one lock, so concurrent sessions can no longer race on the shared
client's open/close cycle: a second caller just waits briefly, and then
benefits from whatever the first caller already warmed in the store's own
internal cache rather than re-downloading independently. The "Refresh Data"
sidebar control was already removed for the same underlying reason (see
dashboard/dashboard_controller.py) -- there is deliberately no manual way to
force this page to bypass any of this and hit the Data Lake directly.

The lock is an ``RLock``, not a plain ``Lock``, because the vendored store's
own methods call each other internally on ``self`` -- e.g.
``portfolios()`` calls ``self.load_reports_by_portfolio()``, and (before the
cache-preferring wrapper above) ``aum_history_for_model_portfolio()`` called
``self.portfolios()``. Since ``self`` is this same patched instance, that
inner call resolves to the *wrapped* method too. A plain ``Lock`` deadlocks
outright the instant one locked method calls another locked method from
within the same request (confirmed in production: every single call to
``aum_history_for_model_portfolio`` hung permanently, holding the lock and
blocking every other session's Portfolio Dashboard page, until the container
was restarted) -- ``RLock`` lets the same thread re-enter without deadlocking
while still fully serializing across different threads/sessions.

``ttl`` on the cached controller is unrelated to the above -- it bounds how
long SilverStore's *own* internal memoisation for the (still live, lock-
serialized) methods above is allowed to accumulate in memory before Streamlit
tears the controller down and rebuilds it fresh.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st
from portfolio_dashboard import silver_cache_model
from portfolio_dashboard.dashboard.dashboard_controller import DashboardController

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 3600

# Store methods that still touch the shared live Data Lake client directly
# (no SQLite cache in front of them) and so must be serialized -- see the
# module docstring for why sharing the store makes this necessary.
_LOCKED_STORE_METHODS = ("has_data", "factor_history", "sector_history", "portfolios")

# RLock, not Lock -- see the module docstring's "The lock is an RLock" section.
_store_lock = threading.RLock()


def _locked(real_method: Any) -> Any:
    """Serialize calls to a shared SilverStore method across all sessions."""

    def _call(*args: Any, **kwargs: Any) -> Any:
        with _store_lock:
            return real_method(*args, **kwargs)

    return _call


def _cache_preferring_load(real_load: Any) -> Any:
    """Wrap SilverStore.load_reports_by_portfolio to prefer the local SQLite cache."""
    locked_real_load = _locked(real_load)

    def _load() -> dict[str, dict[str, object]]:
        cached = silver_cache_model.load_reports()
        if cached is not None:
            return cached
        # Cold start only: no scheduler sync has ever completed. Falls
        # through to SilverStore's real (slower, possibly-timing-out, but now
        # lock-serialized) listing so the page still works before the first
        # daily sync -- exactly what it already did before this wrapper
        # existed, minus the concurrency race.
        return locked_real_load()

    return _load


def _cache_preferring_summary_history(real_summary_history: Any) -> Any:
    """Wrap SilverStore.summary_history to prefer the local SQLite cache."""
    locked_real = _locked(real_summary_history)

    def _summary_history(portfolio: str) -> Any:
        cached = silver_cache_model.load_summary_history(portfolio)
        if cached is not None:
            return cached
        return locked_real(portfolio)

    return _summary_history


def _aum_history_from_frame(frame: pd.DataFrame) -> list[tuple[date, float]]:
    """Reshape a summary-history frame into sorted (date, aum) pairs, same as SilverStore.aum_history_for_model_portfolio."""
    valid = frame[["as_of", "end_mkt_val"]].dropna(subset=["end_mkt_val"])
    if valid.empty:
        return []
    dates = pd.to_datetime(valid["as_of"]).dt.date.tolist()
    values = valid["end_mkt_val"].astype(float).tolist()
    return sorted(zip(dates, values), key=lambda pair: pair[0])


def _cache_preferring_aum_history(real_aum_history: Any) -> Any:
    """
    Wrap SilverStore.aum_history_for_model_portfolio to prefer the local SQLite cache.

    Derived from the same cached data ``summary_history`` already persists
    (``end_mkt_val``/``as_of`` per date, keyed by portfolio) plus the cached
    portfolio-name list, replicating the real method's own prefix-match
    lookup (see ``SilverStore.aum_history_for_model_portfolio``) -- so this
    never needs its own SQLite table, and, just as importantly, never calls
    back into another locked store method (the real method's ``self.portfolios()``
    call is exactly what made a plain, non-reentrant lock deadlock -- see the
    module docstring).
    """
    locked_real = _locked(real_aum_history)

    def _aum_history(model_portfolio: str) -> list[tuple[date, float]]:
        cached_reports = silver_cache_model.load_reports()
        if cached_reports is None:
            return locked_real(model_portfolio)
        prefix = model_portfolio.strip().upper()
        portfolio = next((name for name in sorted(cached_reports) if name.upper().startswith(prefix)), None)
        if portfolio is None:
            return []
        frame = silver_cache_model.load_summary_history(portfolio)
        if frame is None:
            return locked_real(model_portfolio)
        return _aum_history_from_frame(frame)

    return _aum_history


@st.cache_resource(show_spinner=False, ttl=_CACHE_TTL_SECONDS)
def _get_dashboard_controller() -> DashboardController:
    controller = DashboardController()
    store = controller.store
    store.load_reports_by_portfolio = _cache_preferring_load(store.load_reports_by_portfolio)
    store.summary_history = _cache_preferring_summary_history(store.summary_history)
    store.aum_history_for_model_portfolio = _cache_preferring_aum_history(store.aum_history_for_model_portfolio)
    for method_name in _LOCKED_STORE_METHODS:
        setattr(store, method_name, _locked(getattr(store, method_name)))
    return controller


try:
    _get_dashboard_controller().run()
except Exception:  # noqa: BLE001
    # A Data Lake connectivity failure here (e.g. the silver layer's storage
    # account being unreachable from this deployment's network) would
    # otherwise crash this whole page with a raw traceback. Logged so it's
    # still visible on the Logs page / in-memory capture, but shown to the
    # user as a plain, actionable message instead.
    logger.exception("Portfolio Dashboard failed to load")
    st.error(
        "Couldn't load the Portfolio Dashboard's data right now — the silver-layer Data Lake "
        "may be unreachable from this deployment. Check the **Logs** page for the full error, "
        "or try again in a moment."
    )

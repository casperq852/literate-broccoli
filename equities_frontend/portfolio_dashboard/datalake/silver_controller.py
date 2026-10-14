"""Silver-layer read store backing the dashboard.

:class:`SilverStore` is the dashboard's read store: it reads the parsed report
parquet tables from the silver container and exposes ``has_data``, ``portfolios``
and the three history frames plus :meth:`load_reports_by_portfolio` for the
current-period tabs. Reads are served from the per-portfolio *aggregation* files written by the
medallion refresher (``EQT/portfolio/<code>/aggregation/<slug>__<key>.parquet``),
which stack every date of one component into a single parquet, so the overview
and trends load one file per component instead of one file per date. When a
component has no aggregation file yet the store falls back to the individual
date-partitioned silver parquet.

Rebuilding a report object from parquet is CPU-bound, so reconstruction is kept
lazy and demand-driven: the current-period overview only rebuilds each
component's *latest* date (via a pushed-down date filter), while the trend views
rebuild a component's full history the first time that portfolio is opened.
Downloaded aggregation bytes and reconstructed reports are memoised per
component so a single store (held across Streamlit reruns) reads each parquet at
most once; the controller calls :meth:`invalidate` after a refresh to pick up
new data. The silver listing and parquet downloads run through the async Data
Lake manager, driven to completion on the shared background loop (see
:func:`run_async`) so the store keeps a plain synchronous surface for Streamlit.
"""

from __future__ import annotations

import asyncio
import datetime

import pandas as pd

from portfolio_dashboard.datalake import helpers
from portfolio_dashboard.datalake.datalake_controller import DatalakeClient, run_async
from portfolio_dashboard.datalake.silver_model import (
    aggregation_bytes_to_reports_by_date,
    aggregation_dates,
    aggregation_report_for_date,
    parquet_bytes_to_report,
)

_SUMMARY_COLUMNS = [
    "as_of",
    "portfolio",
    "benchmark",
    "te_pct",
    "beta",
    "port_return",
    "bench_return",
    "active_return",
    "end_mkt_val",
]
_FACTOR_COLUMNS = ["as_of", "factor_label", "category", "active_exposure", "contribution_pct"]
_SECTOR_COLUMNS = ["as_of", "sector", "wgt_d"]

# A component is one ``(code, slug, key)`` triple, for example
# ``("ADAAF", "ADAAF_FONDS", "te")``. Its reports are cached as ``{date: report}``.
_Combo = tuple[str, str, str]
_ComboSource = tuple[helpers.AggregationEntry | None, list[helpers.SilverEntry]]


class SilverStore:
    """Read parsed reports and history frames from the silver container.

    Attributes:
        client: The Data Lake facade used to list and download silver parquet.
    """

    def __init__(self, client: DatalakeClient | None = None) -> None:
        """Wire the store to a Data Lake client.

        Args:
            client: An existing :class:`DatalakeClient`, or ``None`` to build one.
        """
        self.client: DatalakeClient = client if client is not None else DatalakeClient()
        self._files: list[str] | None = None
        self._combo_index: dict[_Combo, _ComboSource] | None = None
        self._agg_bytes: dict[_Combo, bytes] = {}
        self._combo_dates: dict[_Combo, list[str]] = {}
        self._combo_reports: dict[_Combo, dict[str, object]] = {}
        self._fully_loaded: set[_Combo] = set()

    @property
    def environment(self) -> str:
        """Return the datalake environment backing the silver layer.

        Returns:
            str: The environment name, for example ``development`` or ``production``.
        """
        return self.client.environment_for("silver")

    def invalidate(self) -> None:
        """Drop every cached listing, download and reconstructed report."""
        self._files = None
        self._combo_index = None
        self._agg_bytes = {}
        self._combo_dates = {}
        self._combo_reports = {}
        self._fully_loaded = set()

    # ── async I/O primitives ─────────────────────────────────────────────────

    async def _list_silver_parquet(self) -> list[str]:
        """List every silver parquet path using the async manager."""
        async with self.client:
            return [path for path in await self.client.list_files("silver") if path.endswith(".parquet")]

    async def _download_paths(self, paths: list[str]) -> list[bytes]:
        """Download the given silver parquet paths concurrently in one session.

        Args:
            paths: The silver parquet paths to download.

        Returns:
            list[bytes]: The downloaded payloads, aligned to ``paths``.
        """
        async with self.client:
            return list(await asyncio.gather(*(self.client.download("silver", path) for path in paths)))

    # ── lazy priming ─────────────────────────────────────────────────────────

    def _silver_files(self) -> list[str]:
        """Return, and memoise, every silver parquet path."""
        if self._files is None:
            self._files = run_async(self._list_silver_parquet())
        return self._files

    def _index(self) -> dict[_Combo, _ComboSource]:
        """Return, and memoise, the per-component source index.

        Each ``(code, slug, key)`` component maps to its aggregation file (when
        one exists) and the list of its date-partitioned silver entries (used as
        the fallback source). A component is present when either source exists.

        Returns:
            dict[_Combo, _ComboSource]: Component to ``(aggregation, date_entries)``.
        """
        if self._combo_index is None:
            files = self._silver_files()
            aggregations: dict[_Combo, helpers.AggregationEntry] = {}
            for path in files:
                entry = helpers.parse_aggregation_path(path)
                if entry is not None:
                    aggregations[(entry.code, entry.slug, entry.key)] = entry
            per_date: dict[_Combo, list[helpers.SilverEntry]] = {}
            for path in files:
                entry = helpers.parse_silver_path(path)
                if entry is not None:
                    per_date.setdefault((entry.code, entry.slug, entry.key), []).append(entry)
            combos = set(aggregations) | set(per_date)
            self._combo_index = {combo: (aggregations.get(combo), per_date.get(combo, [])) for combo in combos}
        return self._combo_index

    def _ensure_agg_bytes(self, combos: list[_Combo]) -> None:
        """Download and cache the aggregation payloads for the given components.

        Only components that have an aggregation file and are not already cached
        are fetched, and every download runs in a single async session.

        Args:
            combos: The components whose aggregation bytes are needed.
        """
        index = self._index()
        to_fetch = [
            combo
            for combo in dict.fromkeys(combos)
            if index.get(combo, (None, []))[0] is not None and combo not in self._agg_bytes
        ]
        if not to_fetch:
            return
        paths = [index[combo][0].path for combo in to_fetch]  # type: ignore[union-attr]
        payloads = run_async(self._download_paths(paths))
        for combo, data in zip(to_fetch, payloads):
            self._agg_bytes[combo] = data

    def _dates_for(self, combo: _Combo) -> list[str]:
        """Return, and memoise, the sorted dates available for a component.

        Aggregation-backed components read only the date column (no report is
        reconstructed); fallback components take their dates from the silver
        path partitions, so neither path is expensive.

        Args:
            combo: The component.

        Returns:
            list[str]: Ascending eight-digit ``YYYYMMDD`` dates.
        """
        if combo in self._combo_dates:
            return self._combo_dates[combo]
        aggregation, date_entries = self._index().get(combo, (None, []))
        if aggregation is not None:
            self._ensure_agg_bytes([combo])
            dates = aggregation_dates(self._agg_bytes[combo])
        else:
            dates = sorted(entry.date for entry in date_entries)
        self._combo_dates[combo] = dates
        return dates

    def _prefetch_fallback_reports(self, pairs: list[tuple[_Combo, str]]) -> None:
        """Batch-download and reconstruct specific ``(component, date)`` reports.

        Used before the overview reconstructs single dates so that fallback
        (non-aggregated) components never open one async session per file.

        Args:
            pairs: The ``(component, date)`` reports to warm into the cache.
        """
        index = self._index()
        needed: list[tuple[_Combo, str, str]] = []
        for combo, date in pairs:
            if date in self._combo_reports.get(combo, {}):
                continue
            _, date_entries = index.get(combo, (None, []))
            path = {entry.date: entry.path for entry in date_entries}.get(date)
            if path is not None:
                needed.append((combo, date, path))
        if not needed:
            return
        payloads = run_async(self._download_paths([path for _, _, path in needed]))
        for (combo, date, _), data in zip(needed, payloads):
            self._combo_reports.setdefault(combo, {})[date] = parquet_bytes_to_report(data)

    def _report_for(self, combo: _Combo, date: str) -> object | None:
        """Return, and memoise, one component's report for a single date.

        Aggregation-backed components reconstruct just the requested date via a
        pushed-down filter; fallback components download that date's parquet
        (unless a batch prefetch already cached it).

        Args:
            combo: The component.
            date: The eight-digit ``YYYYMMDD`` date.

        Returns:
            object | None: The report, or ``None`` when the date is absent.
        """
        cache = self._combo_reports.setdefault(combo, {})
        if date in cache:
            return cache[date]
        aggregation, date_entries = self._index().get(combo, (None, []))
        if aggregation is not None:
            self._ensure_agg_bytes([combo])
            report = aggregation_report_for_date(self._agg_bytes[combo], date)
        else:
            path = {entry.date: entry.path for entry in date_entries}.get(date)
            report = parquet_bytes_to_report(run_async(self._download_paths([path]))[0]) if path is not None else None
        cache[date] = report
        return report

    def _all_reports_for(self, combo: _Combo | None) -> dict[str, object]:
        """Return, and memoise, every date's report for one component.

        Used by the trend views, which need the full history. The reconstruction
        happens once per component and reuses any aggregation bytes already
        downloaded for the overview.

        Args:
            combo: The component, or ``None`` when it does not exist.

        Returns:
            dict[str, object]: Eight-digit ``YYYYMMDD`` date mapped to the report.
        """
        if combo is None:
            return {}
        if combo in self._fully_loaded:
            return self._combo_reports.get(combo, {})
        aggregation, date_entries = self._index().get(combo, (None, []))
        if aggregation is not None:
            self._ensure_agg_bytes([combo])
            reports = aggregation_bytes_to_reports_by_date(self._agg_bytes[combo])
        else:
            paths = [entry.path for entry in date_entries]
            payloads = run_async(self._download_paths(paths)) if paths else []
            reports = {entry.date: parquet_bytes_to_report(data) for entry, data in zip(date_entries, payloads)}
        cache = self._combo_reports.setdefault(combo, {})
        cache.update(reports)
        self._fully_loaded.add(combo)
        return dict(cache)

    def _combo_for(self, slug: str, key: str) -> _Combo | None:
        """Return the component matching a portfolio slug and storage key.

        When multiple fund-code folders in the silver layer produce combos with
        the same slug and key (e.g. after a risk-model switch that changed the
        file-name prefix), the combo with the most recent data is preferred so
        that ``factor_history`` and ``sector_history`` stay aligned with the
        latest-date selection used by ``load_reports_by_portfolio``.

        Args:
            slug: The portfolio slug.
            key: The report storage key, for example ``te`` or ``te_factors``.

        Returns:
            _Combo | None: The matching ``(code, slug, key)``, or ``None``.
        """
        matching = [c for c in self._index() if c[1] == slug and c[2] == key]
        if not matching:
            return None
        if len(matching) == 1:
            return matching[0]
        return max(matching, key=lambda c: max(self._dates_for(c), default=""))

    # ── dashboard read surface ───────────────────────────────────────────────

    def has_data(self) -> bool:
        """Report whether any parsed reports exist in the silver layer.

        Returns:
            bool: ``True`` when at least one silver parquet is present.
        """
        return bool(self._silver_files())

    def load_reports_by_portfolio(self) -> dict[str, dict[str, object]]:
        """Load the most recent report set for every portfolio.

        For each portfolio the latest available date wins, mirroring the upload
        flow's "most recent report per slot" behaviour. Only that latest date is
        reconstructed per component, so the overview never rebuilds the full
        stored history.

        Returns:
            dict[str, dict[str, object]]: Portfolio display name mapped to its
            ``{storage_key: report}`` slots.
        """
        index = self._index()
        combos = list(index)
        self._ensure_agg_bytes(combos)
        dates_by_combo = {combo: self._dates_for(combo) for combo in combos}

        by_slug: dict[str, list[_Combo]] = {}
        for combo in combos:
            by_slug.setdefault(combo[1], []).append(combo)

        latest_by_slug: dict[str, str] = {}
        for slug, slug_combos in by_slug.items():
            dates = {date for combo in slug_combos for date in dates_by_combo[combo]}
            if dates:
                latest_by_slug[slug] = max(dates)

        self._prefetch_fallback_reports(
            [
                (combo, latest_by_slug[combo[1]])
                for combo in combos
                if combo[1] in latest_by_slug
                and index[combo][0] is None
                and latest_by_slug[combo[1]] in dates_by_combo[combo]
            ]
        )

        result: dict[str, dict[str, object]] = {}
        for slug, slug_combos in by_slug.items():
            latest = latest_by_slug.get(slug)
            if latest is None:
                continue
            for combo in slug_combos:
                if latest not in dates_by_combo[combo]:
                    continue
                report = self._report_for(combo, latest)
                if report is None:
                    continue
                portfolio = report.metadata.portfolio or "Unknown"  # type: ignore[attr-defined]
                result.setdefault(portfolio, {})[helpers.report_storage_key(report)] = report
        return result

    def portfolios(self) -> list[str]:
        """List the portfolio display names present in the silver layer.

        Returns:
            list[str]: Portfolio names ordered alphabetically.
        """
        return sorted(self.load_reports_by_portfolio().keys())

    def summary_history(self, portfolio: str) -> pd.DataFrame:
        """Return the per-date summary metrics for a portfolio.

        Args:
            portfolio: The portfolio display name.

        Returns:
            pd.DataFrame: Rows ordered by ``as_of`` with tracking-error, beta,
            return, and end-of-day market value columns.
        """
        slug = helpers.portfolio_slug(portfolio)
        te_by_date = self._all_reports_for(self._combo_for(slug, "te"))
        attr_by_date = self._all_reports_for(self._combo_for(slug, "attr"))
        factor_by_date = self._all_reports_for(self._combo_for(slug, "factor"))

        rows: list[dict[str, object]] = []
        for date in sorted(set(te_by_date) | set(attr_by_date) | set(factor_by_date)):
            te = te_by_date.get(date)
            attr = attr_by_date.get(date)
            factor = factor_by_date.get(date)
            anchor = te or attr or factor
            if anchor is None:
                continue
            rows.append(
                {
                    "as_of": helpers.date_to_iso(date),
                    "portfolio": portfolio,
                    "benchmark": anchor.metadata.benchmark,  # type: ignore[attr-defined]
                    "te_pct": te.portfolio_te if te is not None else None,  # type: ignore[attr-defined]
                    "beta": te.portfolio_beta if te is not None else None,  # type: ignore[attr-defined]
                    "port_return": attr.portfolio_return if attr is not None else None,  # type: ignore[attr-defined]
                    "bench_return": attr.benchmark_return if attr is not None else None,  # type: ignore[attr-defined]
                    "active_return": attr.active_return if attr is not None else None,  # type: ignore[attr-defined]
                    "end_mkt_val": factor.portfolio_end_mkt_val if factor is not None else None,  # type: ignore[attr-defined]
                }
            )
        frame = pd.DataFrame(rows, columns=_SUMMARY_COLUMNS)
        # Forward-fill (then back-fill) end_mkt_val for dates where the factor
        # report pre-dates the End Mkt Val column addition so older history still
        # gets a usable approximation from the nearest available value.
        if not frame.empty:
            frame["end_mkt_val"] = frame["end_mkt_val"].ffill().bfill()
        return frame

    def aum_for_model_portfolio(self, model_portfolio: str) -> float | None:
        """Return the most recent end-of-day market value for a model portfolio.

        Searches all known silver portfolios for one whose display name starts
        with ``model_portfolio`` (case-insensitive) and returns the latest
        non-null ``end_mkt_val`` from that portfolio's summary history.  This
        maps the short transaction-CSV model-portfolio names (e.g. "EQEU ALPHA")
        to the full Bloomberg portfolio names stored in the silver layer (e.g.
        "EQEU ALPHA_AEIIAEF").

        Args:
            model_portfolio: The model-portfolio prefix to match.

        Returns:
            float | None: The latest AUM in the report's base currency, or
            ``None`` when no match or no value is found.
        """
        prefix = model_portfolio.strip().upper()
        for port in self.portfolios():
            if port.upper().startswith(prefix):
                df = self.summary_history(port)
                if not df.empty:
                    valid = df["end_mkt_val"].dropna()
                    if not valid.empty:
                        return float(valid.iloc[-1])
        return None

    def aum_history_for_model_portfolio(self, model_portfolio: str) -> list[tuple[datetime.date, float]]:
        """Return sorted (as_of_date, aum) pairs for the portfolio matching the given prefix.

        Searches for a silver portfolio whose display name starts with
        ``model_portfolio`` (case-insensitive) and returns all non-null
        ``end_mkt_val`` values with their dates, sorted ascending.  Used by the
        Transacties tab to look up the AUM closest to each trade date so that bps
        can be calculated relative to the portfolio size at the time.

        Args:
            model_portfolio: The model-portfolio prefix to match, e.g. "EQEU ALPHA".

        Returns:
            list[tuple[datetime.date, float]]: Ascending (date, aum_eur) pairs,
            or an empty list when no match or no AUM values are found.
        """
        prefix = model_portfolio.strip().upper()
        for port in self.portfolios():
            if port.upper().startswith(prefix):
                df = self.summary_history(port)
                valid = df[["as_of", "end_mkt_val"]].dropna(subset=["end_mkt_val"])
                if valid.empty:
                    return []
                dates = pd.to_datetime(valid["as_of"]).dt.date.tolist()
                values = valid["end_mkt_val"].astype(float).tolist()
                return sorted(zip(dates, values), key=lambda pair: pair[0])
        return []

    def factor_history(self, portfolio: str) -> pd.DataFrame:
        """Return the per-date active factor exposures for a portfolio.

        Reads the same ``te_factors`` component that the Factor Risk Table and
        ``load_reports_by_portfolio`` use for the current period, via
        :meth:`_combo_for`. This keeps the trend and snapshot views aligned
        with the current-period active factor exposures instead of blending
        rows from multiple fund-code combos.

        Args:
            portfolio: The portfolio display name.

        Returns:
            pd.DataFrame: Factor rows ordered by ``as_of`` and factor label.
        """
        by_date = self._all_reports_for(self._combo_for(helpers.portfolio_slug(portfolio), "te_factors"))
        rows: list[dict[str, object]] = []
        for date in sorted(by_date):
            iso = helpers.date_to_iso(date)
            for record in by_date[date].factors:  # type: ignore[attr-defined]
                rows.append(
                    {
                        "as_of": iso,
                        "factor_label": record.factor_label,
                        "category": record.category,
                        "active_exposure": record.active_exposure,
                        "contribution_pct": record.contribution_pct,
                    }
                )
        frame = pd.DataFrame(rows, columns=_FACTOR_COLUMNS)
        return frame.sort_values(["as_of", "factor_label"]) if not frame.empty else frame

    def sector_history(self, portfolio: str) -> pd.DataFrame:
        """Return the per-date active sector weights for a portfolio.

        Args:
            portfolio: The portfolio display name.

        Returns:
            pd.DataFrame: Sector rows ordered by ``as_of`` and sector.
        """
        by_date = self._all_reports_for(self._combo_for(helpers.portfolio_slug(portfolio), "te"))
        rows: list[dict[str, object]] = []
        for date in sorted(by_date):
            iso = helpers.date_to_iso(date)
            for record in by_date[date].sectors:  # type: ignore[attr-defined]
                rows.append({"as_of": iso, "sector": record.name, "wgt_d": record.wgt_d})
        frame = pd.DataFrame(rows, columns=_SECTOR_COLUMNS)
        return frame.sort_values(["as_of", "sector"]) if not frame.empty else frame

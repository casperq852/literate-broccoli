"""Gewichten tab view — nudge position weights against the benchmark.

Ported from the standalone "Gewichten" weight editor. Unlike the original,
which sourced portfolio/benchmark weights from an uploaded Excel file, this
version seeds the table straight from the selected portfolio's tracking-error
report (the same source the Risk & Tracking Error tab reads), so no separate
upload is needed and it works for every portfolio in the silver layer.

The table only ever holds *active* positions: names actively overweight, or
held (nonzero current weight) at less than the benchmark weight. Names simply
not held — the bulk of any benchmark-covered TE report — are excluded rather
than shown as a passive full underweight, since they reflect no deliberate
position decision.
"""

from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from portfolio_dashboard.reports.reports_model import TrackingErrorReport

_STEP_OPTIONS: dict[str, float] = {"0.10%": 0.001, "0.25%": 0.0025, "0.50%": 0.005, "1.00%": 0.01}
_ACTIVE_EPS: float = 0.0001  # active weight below this counts as flat, for OW/UW counts and trade counts
_PCT_EXPORT_COLS: tuple[str, ...] = ("Huidig", "Benchmark", "Voorgesteld", "Active", "Trade")

_EDITOR_KEY: str = "weights_editor"
_HIDDEN_KEY: str = "weights_hidden"
_SOURCE_KEY: str = "weights_source"


# ── data prep ────────────────────────────────────────────────────────────────


def _sector_industry_map(te_report: TrackingErrorReport) -> dict[str, tuple[str, str]]:
    """Map each security name to its (sector, industry group).

    Only the 4-level GICS report format (sector -> industry group -> security)
    carries this structure; legacy 2-level reports yield an empty mapping.

    Args:
        te_report (TrackingErrorReport): The tracking-error report to walk.

    Returns:
        dict[str, tuple[str, str]]: Security name mapped to (sector, industry group).
    """
    mapping: dict[str, tuple[str, str]] = {}
    for sector in te_report.sectors:
        for group in te_report.children_of(sector.name, parent_level=1):
            for security in te_report.children_of(group.name, parent_level=2):
                mapping[security.name] = (sector.name, group.name)
    return mapping


def _group_benchmark_totals(te_report: TrackingErrorReport, grouping: str) -> dict[str, float]:
    """Return each Sector's or Industry group's true total benchmark weight.

    Read directly from the report's own sector/industry-group aggregate rows
    rather than summed from the (filtered, active-only) editor table, so the
    figure reflects the whole benchmark, not just the shown active bets.

    Args:
        te_report (TrackingErrorReport): Tracking-error report for the selected portfolio.
        grouping (str): Either ``"Sector"`` or ``"Industry"``.

    Returns:
        dict[str, float]: Group name mapped to its benchmark weight fraction.
    """
    records = te_report.sectors if grouping == "Sector" else te_report.industry_groups
    return {record.name: ((record.wgt_p or 0.0) - (record.wgt_d or 0.0)) / 100.0 for record in records}


def _group_true_active_totals(te_report: TrackingErrorReport, grouping: str) -> dict[str, float]:
    """Return each Sector's or Industry group's true total active weight.

    Read directly from the report's own sector/industry-group aggregate rows,
    covering every constituent — including names not held at all, which are
    excluded from the editor table but still count as a real (unedited)
    underweight at the group level. Combine with the live edits made to the
    visible active positions in that group to get the group's current active
    weight (see the position-list loop in :func:`render_weights_tab`).

    Args:
        te_report (TrackingErrorReport): Tracking-error report for the selected portfolio.
        grouping (str): Either ``"Sector"`` or ``"Industry"``.

    Returns:
        dict[str, float]: Group name mapped to its active weight fraction.
    """
    records = te_report.sectors if grouping == "Sector" else te_report.industry_groups
    return {record.name: (record.wgt_d or 0.0) / 100.0 for record in records}


def _compute_scaling(frame: pd.DataFrame) -> pd.Series:
    """Rescale each position's active weight against the portfolio's typical bet size.

    100% means a position's active weight matches the average absolute active
    weight among all positions that currently carry one (nonzero Active);
    positions with no active weight score 0%. Every row in the table is
    already an active bet by construction (see :func:`_init_editor_frame`),
    but edits can still zero one out, so the mask is kept rather than assumed.

    Args:
        frame (pd.DataFrame): Editor table with an Active column.

    Returns:
        pd.Series: Scaling percentage per row.
    """
    active = frame["Active"]
    active_mask = active.abs() > _ACTIVE_EPS
    if not active_mask.any():
        return pd.Series(0.0, index=frame.index)
    avg_abs_active = active[active_mask].abs().mean()
    if avg_abs_active <= 1e-9:
        return pd.Series(0.0, index=frame.index)
    return (active / avg_abs_active) * 100


def _recalc(frame: pd.DataFrame) -> pd.DataFrame:
    """Recompute Active and Scaling after a Voorgesteld edit, in place.

    Args:
        frame (pd.DataFrame): Editor table with an updated Voorgesteld column.

    Returns:
        pd.DataFrame: The same frame, with Active and Scaling refreshed.
    """
    frame["Active"] = frame["Voorgesteld"] - frame["Benchmark"]
    frame["Scaling"] = _compute_scaling(frame)
    return frame


def _init_editor_frame(te_report: TrackingErrorReport) -> pd.DataFrame:
    """Build the starting weight-editor table from a tracking-error report.

    Weight fractions are derived from the report's percentage-point fields:
    ``Huidig`` from ``wgt_p`` and ``Benchmark`` from ``wgt_p - wgt_d``. Only
    active positions are kept — overweight, or held at less than benchmark —
    dropping names that are simply not held (a passive full underweight, not
    a deliberate one).

    Args:
        te_report (TrackingErrorReport): Tracking-error report for the selected portfolio.

    Returns:
        pd.DataFrame: One row per active security, with Positie, optionally
        Sector and Industry, Huidig, Benchmark, Voorgesteld, Active and
        Scaling columns.
    """
    sector_map = _sector_industry_map(te_report)
    rows: list[dict[str, object]] = []
    for record in te_report.securities:
        wgt_p = (record.wgt_p or 0.0) / 100.0
        wgt_d = (record.wgt_d or 0.0) / 100.0
        is_overweight = wgt_d > _ACTIVE_EPS
        is_deliberate_underweight = wgt_d < -_ACTIVE_EPS and wgt_p > _ACTIVE_EPS
        if not (is_overweight or is_deliberate_underweight):
            continue
        sector, industry = sector_map.get(record.name, (None, None))
        rows.append(
            {
                "Positie": record.name,
                "Sector": sector,
                "Industry": industry,
                "Huidig": wgt_p,
                "Benchmark": wgt_p - wgt_d,
                "Voorgesteld": wgt_p,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    if frame["Sector"].isna().all():
        frame = frame.drop(columns=["Sector", "Industry"])
    return _recalc(frame)


# ── session-state mutations (position edits) ────────────────────────────────


def _adjust_weight(index: int, delta: float) -> None:
    """Nudge one position's proposed weight and rerun.

    Args:
        index (int): Row index into the session-state editor frame.
        delta (float): Signed weight-fraction change to apply.
    """
    frame = st.session_state[_EDITOR_KEY]
    frame.loc[index, "Voorgesteld"] = max(0.0, frame.loc[index, "Voorgesteld"] + delta)
    st.session_state[_EDITOR_KEY] = _recalc(frame)
    st.rerun()


def _hide_position(name: str) -> None:
    """Zero out a position's proposed weight and mark it hidden, then rerun.

    Args:
        name (str): The position's ``Positie`` value.
    """
    frame = st.session_state[_EDITOR_KEY]
    frame.loc[frame["Positie"] == name, "Voorgesteld"] = 0.0
    st.session_state[_EDITOR_KEY] = _recalc(frame)
    st.session_state[_HIDDEN_KEY].add(name)
    st.rerun()


# ── HTML fragments ───────────────────────────────────────────────────────────


def _render_active_bar(scaling_pct: float, active_pct: float) -> str:
    """Render an inline bar for one position's scaled active weight.

    Args:
        scaling_pct (float): Scaling value, 100 = equal-active baseline.
        active_pct (float): Active weight in percentage points.

    Returns:
        str: An HTML snippet for ``st.markdown(..., unsafe_allow_html=True)``.
    """
    display_pct = min(abs(scaling_pct), 200)
    bar_width = int(display_pct / 2)
    color = "#28a745" if scaling_pct > 0 else "#dc3545" if scaling_pct < 0 else "#6c757d"
    return (
        '<div style="display:flex;align-items:center;gap:8px;">'
        '<div style="width:100px;height:16px;background:#e9ecef;border-radius:3px;overflow:hidden;">'
        f'<div style="width:{bar_width}px;height:100%;background:{color};"></div></div>'
        f'<span style="color:{color};font-size:12px;min-width:45px;">{scaling_pct:+.0f}%</span>'
        f'<span style="color:#666;font-size:11px;">({active_pct:+.2f}%)</span></div>'
    )


def _render_group_active_bar(active_pct: float) -> str:
    """Render a centered diverging bar for a group's total active weight.

    Args:
        active_pct (float): Group active weight in percentage points.

    Returns:
        str: An HTML snippet for ``st.markdown(..., unsafe_allow_html=True)``.
    """
    bar_width = int(min(abs(active_pct), 5) * 10)
    color = "#28a745" if active_pct > 0.01 else "#dc3545" if active_pct < -0.01 else "#6c757d"
    side = "left" if active_pct >= 0 else "right"
    radius = "0 3px 3px 0" if active_pct >= 0 else "3px 0 0 3px"
    return (
        '<div style="display:flex;align-items:center;gap:8px;">'
        '<div style="width:100px;height:16px;background:#e9ecef;border-radius:3px;position:relative;">'
        '<div style="position:absolute;left:50%;top:0;width:1px;height:100%;background:#999;"></div>'
        f'<div style="position:absolute;{side}:50%;top:0;width:{bar_width}px;height:100%;'
        f'background:{color};border-radius:{radius};"></div></div>'
        f'<span style="color:{color};font-size:12px;font-weight:600;">{active_pct:+.2f}%</span></div>'
    )


def _render_position_row(index: int, row: pd.Series, step: float, key_prefix: str) -> None:
    """Render one position's editable row (label, weights, +/-, hide).

    Args:
        index (int): Row index into the session-state editor frame.
        row (pd.Series): The row's data.
        step (float): Weight-fraction step for the +/- buttons.
        key_prefix (str): Prefix keeping widget keys unique across groupings.
    """
    cols = st.columns([2.2, 1.3, 3, 0.9, 0.4])
    with cols[0]:
        st.markdown(f"**{row['Positie']}**")
    with cols[1]:
        st.caption(f"{row['Voorgesteld'] * 100:.2f}% (B: {row['Benchmark'] * 100:.2f}%)")
    with cols[2]:
        st.markdown(_render_active_bar(row["Scaling"], row["Active"] * 100), unsafe_allow_html=True)
    with cols[3]:
        minus_col, plus_col = st.columns(2)
        with minus_col:
            if st.button("➖", key=f"m_{key_prefix}_{index}"):
                _adjust_weight(index, -step)
        with plus_col:
            if st.button("➕", key=f"p_{key_prefix}_{index}"):
                _adjust_weight(index, step)
    with cols[4]:
        if st.button("✕", key=f"rm_{key_prefix}_{index}", help="Verberg positie (weight naar 0)"):
            _hide_position(row["Positie"])


# ── export ────────────────────────────────────────────────────────────────────


def _export_bytes(frame: pd.DataFrame) -> bytes:
    """Build an XLSX export of the proposed weights and resulting trades.

    Args:
        frame (pd.DataFrame): Full editor table (hidden positions already at Voorgesteld=0).

    Returns:
        bytes: The XLSX file content.
    """
    export = frame.copy()
    export["Trade"] = export["Voorgesteld"] - export["Huidig"]
    ordered_cols = ["Positie"]
    if "Sector" in export.columns:
        ordered_cols += ["Sector", "Industry"]
    ordered_cols += list(_PCT_EXPORT_COLS)
    export = export[ordered_cols].sort_values("Active", key=abs, ascending=False)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        export.to_excel(writer, sheet_name="Weights", index=False)
        workbook = writer.book
        worksheet = writer.sheets["Weights"]
        pct_format = workbook.add_format({"num_format": "0.00%"})
        for col_idx, col_name in enumerate(export.columns):
            if col_name in _PCT_EXPORT_COLS:
                worksheet.set_column(col_idx, col_idx, 12, pct_format)
            else:
                worksheet.set_column(col_idx, col_idx, 26)

        pd.DataFrame(
            [
                {"Metric": "Som Voorgesteld", "Value": export["Voorgesteld"].sum()},
                {"Metric": "Som Active", "Value": export["Active"].sum()},
                {"Metric": "Turnover", "Value": export["Trade"].abs().sum()},
                {"Metric": "# Buys", "Value": int((export["Trade"] > _ACTIVE_EPS).sum())},
                {"Metric": "# Sells", "Value": int((export["Trade"] < -_ACTIVE_EPS).sum())},
            ]
        ).to_excel(writer, sheet_name="Summary", index=False)
    return buffer.getvalue()


# ── tab renderer ───────────────────────────────────────────────────────────────


def render_weights_tab(*, te_report: TrackingErrorReport | None, is_master: bool) -> None:
    """Render the Gewichten (weight editor) tab.

    Lets the user nudge individual position weights against the benchmark,
    optionally grouped by Sector or Industry, seeded from the selected
    portfolio's tracking-error report. Positions can be hidden (sold to zero)
    and restored; the result can be exported to Excel.

    Args:
        te_report (TrackingErrorReport | None): Tracking-error report for the selected portfolio.
        is_master (bool): Whether the active portfolio is a master/fund-level
            portfolio without security-level TE rows.
    """
    st.subheader("Gewichten Aanpassen")
    st.caption(
        "Toont alleen actieve posities: overwogen namen, en gehouden namen die bewust onderwogen zijn "
        "t.o.v. de benchmark. Namen die simpelweg niet gehouden worden (een passieve underweight) staan er niet in."
    )

    if is_master:
        st.info("Niet beschikbaar voor fonds-/masterportefeuilles — er zijn geen posities op securityniveau.")
        return
    if te_report is None or not te_report.securities:
        st.info("Upload een TE Report voor deze portefeuille om gewichten aan te passen.")
        return

    source_key = (te_report.metadata.portfolio, te_report.metadata.as_of or te_report.metadata.run_date)
    if st.session_state.get(_SOURCE_KEY) != source_key:
        st.session_state[_SOURCE_KEY] = source_key
        st.session_state[_EDITOR_KEY] = _init_editor_frame(te_report)
        st.session_state[_HIDDEN_KEY] = set()

    full: pd.DataFrame = st.session_state[_EDITOR_KEY]
    if full.empty:
        st.info("Geen actieve over- of onderweegposities gevonden in dit TE Report.")
        return

    hidden: set[str] = st.session_state[_HIDDEN_KEY]
    visible = full[~full["Positie"].isin(hidden)].copy()

    # ── summary ──────────────────────────────────────────────────────────────
    sum_cols = st.columns(5)
    sum_cols[0].metric("Som Voorstel", f"{full['Voorgesteld'].sum():.2%}")
    sum_cols[1].metric("Som Active", f"{full['Active'].sum():+.2%}")
    sum_cols[2].metric("Turnover", f"{(full['Voorgesteld'] - full['Huidig']).abs().sum():.2%}")
    sum_cols[3].metric(
        "OW / UW",
        f"{(full['Active'] > _ACTIVE_EPS).sum()} / {(full['Active'] < -_ACTIVE_EPS).sum()}",
    )
    sum_cols[4].metric("Verborgen", len(hidden))

    # ── controls ─────────────────────────────────────────────────────────────
    has_sector = "Sector" in full.columns
    group_options = ["Geen"] + (["Sector", "Industry"] if has_sector else [])
    ctrl1, ctrl2 = st.columns(2)
    with ctrl1:
        grouping = st.selectbox("Groeperen op", group_options, key="weights_grouping")
    with ctrl2:
        step_label = st.selectbox("Stap", list(_STEP_OPTIONS), index=1, key="weights_step")
    step = _STEP_OPTIONS[step_label]

    # ── position list ────────────────────────────────────────────────────────
    with st.container(height=450):
        if grouping == "Geen":
            for index, row in visible.sort_values("Active", key=abs, ascending=False).iterrows():
                _render_position_row(index, row, step, key_prefix="flat")
        else:
            bench_group_weights = _group_benchmark_totals(te_report, grouping)
            true_active_totals = _group_true_active_totals(te_report, grouping)
            for group_name, group_df in sorted(visible.groupby(grouping), key=lambda item: item[0]):
                # True group active weight (whole universe, incl. unheld names not shown
                # below) plus whatever live edits were made to the visible active positions.
                live_edits = (group_df["Voorgesteld"] - group_df["Huidig"]).sum()
                group_active = true_active_totals.get(group_name, 0.0) + live_edits
                header_cols = st.columns([3, 4, 1])
                header_cols[0].markdown(f"**{group_name}** ({len(group_df)})")
                header_cols[1].markdown(_render_group_active_bar(group_active * 100), unsafe_allow_html=True)
                header_cols[2].caption(f"B: {bench_group_weights.get(group_name, 0.0):.1%}")
                for index, row in group_df.sort_values("Active", key=abs, ascending=False).iterrows():
                    _render_position_row(index, row, step, key_prefix=f"{grouping}_{group_name}")
                st.divider()

    # ── actions ──────────────────────────────────────────────────────────────
    # No "Normaliseer naar 100%" here: the table only holds a curated subset of
    # active bets (see _init_editor_frame), not the whole portfolio, so forcing
    # that subset's Voorgesteld to sum to 100% would not be a meaningful action.
    st.markdown("---")
    col_reset, col_equal, col_restore = st.columns(3)
    with col_reset:
        if st.button("Reset naar Huidig", key="btn_weights_reset"):
            full["Voorgesteld"] = full["Huidig"]
            st.session_state[_EDITOR_KEY] = _recalc(full)
            st.session_state[_HIDDEN_KEY] = set()
            st.rerun()
    with col_equal:
        _equal_help = "Maak alle huidige actieve weddenschappen even groot, per richting (OW/UW blijft OW/UW)"
        if st.button("Equal Active", key="btn_weights_equal", help=_equal_help):
            active_mask = full["Active"].abs() > _ACTIVE_EPS
            if active_mask.any():
                avg_abs_active = full.loc[active_mask, "Active"].abs().mean()
                full.loc[active_mask, "Active"] = full.loc[active_mask, "Active"].apply(
                    lambda value: avg_abs_active if value > 0 else -avg_abs_active
                )
                full.loc[active_mask, "Voorgesteld"] = (
                    full.loc[active_mask, "Benchmark"] + full.loc[active_mask, "Active"]
                )
                full["Scaling"] = _compute_scaling(full)
                st.session_state[_EDITOR_KEY] = full
            st.rerun()
    with col_restore:
        if st.button(f"Herstel ({len(hidden)})", key="btn_weights_restore", disabled=not hidden):
            st.session_state[_HIDDEN_KEY] = set()
            st.rerun()

    # ── export ───────────────────────────────────────────────────────────────
    st.markdown("---")
    export_cols = st.columns([1, 3])
    with export_cols[0]:
        st.download_button(
            "📥 Export Gewichten",
            data=_export_bytes(full),
            file_name="gewichten_export.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="btn_weights_export",
        )
    with export_cols[1]:
        st.caption(
            "Exporteert de voorgestelde gewichten naar Excel. Verborgen posities worden opgenomen als volledige verkoop."
        )

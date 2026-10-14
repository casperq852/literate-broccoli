"""Transactions tab view — weekly equity transaction report upload and analysis."""

from __future__ import annotations

import io
import math
from datetime import date, timedelta

import plotly.graph_objects as go
import polars as pl
import streamlit as st

from portfolio_dashboard.dashboard.dashboard_view import render_chart
from portfolio_dashboard.datalake.silver_controller import SilverStore

# ── constants ──────────────────────────────────────────────────────────────────

_TRANS_CODE_MAP: dict[str, str] = {
    "AllocBuyExt": "Buy",
    "AllocSellExt": "Sell",
}

_ALPHA_PORTFOLIOS: list[str] = ["EQEU ALPHA", "EQUS ALPHA", "EQWW ALPHA"]

_BUY_COLOR: str = "#e6b84f"
_SELL_COLOR: str = "#c0395a"

_FONT_FAMILY: str = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif"
_LAYOUT_BASE: dict = dict(
    plot_bgcolor="white",
    paper_bgcolor="white",
    font=dict(family=_FONT_FAMILY, size=11, color="#2c3e50"),
)

_TX_KEY: str = "transactions_df"


# ── helpers ────────────────────────────────────────────────────────────────────


def _fmt_eur(value: float) -> str:
    """Format a euro amount with Dutch thousands-dot notation, e.g. '€ 100.000.000'."""
    return "€ " + f"{value:,.0f}".replace(",", ".")


def _lookup_aum_on_date(aum_history: list[tuple[date, float]], trade_date: date) -> float | None:
    """Binary-search the most recent AUM on or before trade_date.

    Args:
        aum_history: Ascending (date, aum) pairs from the silver store.
        trade_date: The transaction date to look up.

    Returns:
        float | None: Nearest AUM value, or None when history is empty.
    """
    if not aum_history:
        return None
    lo, hi, result = 0, len(aum_history) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if aum_history[mid][0] <= trade_date:
            result = aum_history[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    # If all AUM dates are after this trade date, fall back to the earliest value.
    return result if result is not None else aum_history[0][1]


# ── CSV parser ─────────────────────────────────────────────────────────────────


def _parse_transaction_csv(raw: bytes) -> pl.DataFrame:
    """Parse a Bloomberg equity-transaction report CSV.

    The export wraps each data row in outer double-quotes and escapes inner
    string values as ""-quoted fields.  This pre-processing step strips the
    outer quotes and un-escapes the inner ones so Polars can read the result
    as a normal RFC-4180 CSV.

    Args:
        raw (bytes): Raw file bytes from the uploader.

    Returns:
        pl.DataFrame: Parsed rows, all columns as Utf8.
    """
    text = raw.decode("utf-8-sig")
    lines = text.splitlines()
    if not lines:
        return pl.DataFrame()

    clean_lines: list[str] = [lines[0]]
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('"') and stripped.endswith('"'):
            clean_lines.append(stripped[1:-1].replace('""', '"'))
        else:
            clean_lines.append(stripped)

    return pl.read_csv(
        io.BytesIO("\n".join(clean_lines).encode("utf-8")),
        infer_schema_length=0,
    )


def _load_and_combine(uploaded_files: list) -> pl.DataFrame:
    """Parse, filter, and combine multiple uploaded transaction CSVs.

    Args:
        uploaded_files (list): Streamlit UploadedFile objects.

    Returns:
        pl.DataFrame: Combined, filtered, and typed transaction frame.
    """
    frames: list[pl.DataFrame] = []
    for f in uploaded_files:
        try:
            frame = _parse_transaction_csv(f.read())
            frame = frame.rename({col: col.strip() for col in frame.columns})
            frames.append(frame)
        except Exception as exc:
            st.warning(f"Kon **{f.name}** niet verwerken: {exc}")

    if not frames:
        return pl.DataFrame()

    combined = pl.concat(frames, how="diagonal_relaxed")

    combined = combined.filter(
        pl.col("Trans. code").is_in(list(_TRANS_CODE_MAP.keys()))
        & pl.col("Model portfolio").str.to_uppercase().str.strip_chars().is_in(_ALPHA_PORTFOLIOS)
    )

    if combined.is_empty():
        return pl.DataFrame()

    return combined.with_columns(
        pl.col("Trans. code").replace(_TRANS_CODE_MAP).alias("Direction"),
        pl.col("Trade date").cast(pl.Utf8).str.slice(0, 8).str.to_date(format="%Y%m%d", strict=False).alias("Trade date"),
        pl.col("Payment PC").cast(pl.Float64, strict=False).abs().alias("Payment PC"),
        pl.col("Model portfolio").str.to_uppercase().str.strip_chars().alias("Model portfolio"),
    ).drop_nulls(subset=["Trade date", "Payment PC"])


def _add_bps_column(frame: pl.DataFrame, aum_history: list[tuple[date, float]]) -> pl.DataFrame:
    """Join the nearest-previous AUM for each trade date and compute bps.

    Uses Polars ``join_asof`` with backward strategy so each transaction gets
    the last known AUM on or before its trade date.

    Args:
        frame (pl.DataFrame): Transaction rows with a ``Trade date`` column.
        aum_history (list): Sorted (date, aum) pairs from the silver store.

    Returns:
        pl.DataFrame: Input frame extended with ``aum`` and ``bps`` columns,
        sorted by Trade date.
    """
    if not aum_history or frame.is_empty():
        return frame

    aum_df = pl.DataFrame(
        {
            "Trade date": [d for d, _ in aum_history],
            "aum": [v for _, v in aum_history],
        },
        schema={"Trade date": pl.Date, "aum": pl.Float64},
    )

    return (
        frame.sort("Trade date")
        .join_asof(aum_df, on="Trade date", strategy="backward")
        # Transactions predating all AUM reports fall back to the earliest value.
        .with_columns(pl.col("aum").fill_null(aum_history[0][1]))
        .with_columns((pl.col("Payment PC") / pl.col("aum") * 10_000).alias("bps"))
    )


# ── charts ─────────────────────────────────────────────────────────────────────


def _transaction_bps_chart(frame: pl.DataFrame, portfolio_label: str) -> go.Figure:
    """Grouped Buy/Sell bar chart in bps, using the pre-computed ``bps`` column.

    Securities are sorted by combined bps (largest last).

    Args:
        frame (pl.DataFrame): Filtered rows containing a ``bps`` column.
        portfolio_label (str): Title label.

    Returns:
        go.Figure: Plotly grouped bar chart.
    """
    grouped = frame.group_by(["Security name", "Direction"]).agg(pl.col("bps").sum())

    totals = frame.group_by("Security name").agg(pl.col("bps").sum()).sort("bps")
    securities: list[str] = totals["Security name"].to_list()

    fig = go.Figure()
    for direction, color in (("Buy", _BUY_COLOR), ("Sell", _SELL_COLOR)):
        sub = grouped.filter(pl.col("Direction") == direction)
        val_map: dict[str, float] = dict(zip(sub["Security name"].to_list(), sub["bps"].to_list()))
        y = [val_map.get(sec, 0.0) for sec in securities]
        hover = [f"<b>{sec}</b><br>{direction}: {v:.2f} bps" for sec, v in zip(securities, y)]
        fig.add_trace(
            go.Bar(
                name=direction,
                x=securities,
                y=y,
                marker_color=color,
                hovertemplate="%{customdata}<extra></extra>",
                customdata=hover,
            )
        )

    fig.add_hline(y=0, line_width=1, line_color="#ccc", line_dash="dot")
    fig.update_layout(
        **_LAYOUT_BASE,
        margin=dict(l=60, r=40, t=60, b=120),
        barmode="group",
        title=dict(text=f"Transacties — {portfolio_label}", x=0, font=dict(size=14)),
        xaxis=dict(tickangle=-40, tickfont=dict(size=10)),
        yaxis_title="bps",
        legend=dict(orientation="h", y=1.08, x=0),
        hovermode="x unified",
        height=460,
    )
    return fig


def _transaction_eur_chart(frame: pl.DataFrame, portfolio_label: str) -> go.Figure:
    """Fallback grouped bar chart in EUR when no AUM history is available.

    Args:
        frame (pl.DataFrame): Filtered transaction rows.
        portfolio_label (str): Title label.

    Returns:
        go.Figure: Plotly grouped bar chart denominated in EUR.
    """
    grouped = frame.group_by(["Security name", "Direction"]).agg(pl.col("Payment PC").sum())
    totals = frame.group_by("Security name").agg(pl.col("Payment PC").sum()).sort("Payment PC")
    securities: list[str] = totals["Security name"].to_list()

    fig = go.Figure()
    for direction, color in (("Buy", _BUY_COLOR), ("Sell", _SELL_COLOR)):
        sub = grouped.filter(pl.col("Direction") == direction)
        val_map: dict[str, float] = dict(zip(sub["Security name"].to_list(), sub["Payment PC"].to_list()))
        fig.add_trace(
            go.Bar(
                name=direction,
                x=securities,
                y=[val_map.get(sec, 0.0) for sec in securities],
                marker_color=color,
                hovertemplate="<b>%{x}</b><br>" + direction + ": €%{y:,.0f}<extra></extra>",
            )
        )

    fig.update_layout(
        **_LAYOUT_BASE,
        margin=dict(l=60, r=40, t=60, b=120),
        barmode="group",
        title=dict(text=f"Transacties — {portfolio_label} (EUR)", x=0, font=dict(size=14)),
        xaxis=dict(tickangle=-40, tickfont=dict(size=10)),
        yaxis_title="EUR",
        legend=dict(orientation="h", y=1.08, x=0),
        hovermode="x unified",
        height=460,
    )
    return fig


# ── tab renderer ───────────────────────────────────────────────────────────────


def render_transactions_tab(*, store: SilverStore) -> None:
    """Render the Transacties tab.

    Upload section: one or more weekly Bloomberg equity-transaction CSVs.
    Filter section: portfolio, trade-date range, bps range.
    AUM display: read-only, from the most recent Factor Attribution report.
    Chart: grouped Buy / Sell bars in bps per security.
    Table: filterable by bps with per-date AUM note.

    Args:
        store (SilverStore): Silver read store — used to retrieve AUM history.
    """
    # ── upload ───────────────────────────────────────────────────────────────
    st.subheader("Upload transactiebestanden")
    uploaded = st.file_uploader(
        "Selecteer een of meerdere weekbestanden (.csv)",
        type=["csv"],
        accept_multiple_files=True,
        key="tx_uploader",
        help="Bloomberg equity-transaction report CSV exports (EQEU/EQUS/EQWW Alpha).",
    )

    if uploaded:
        df_all = _load_and_combine(uploaded)
        if not df_all.is_empty():
            st.session_state[_TX_KEY] = df_all
        else:
            st.warning("Geen geldige transacties gevonden in de geüploade bestanden.")

    frame: pl.DataFrame = st.session_state.get(_TX_KEY, pl.DataFrame())

    if frame.is_empty():
        st.info(
            "Upload een of meerdere weekbestanden via de knop hierboven. "
            "Alleen **AllocBuyExt** en **AllocSellExt** transacties van "
            "EQEU Alpha, EQUS Alpha en EQWW Alpha worden meegenomen."
        )
        return

    st.divider()

    # ── portfolio + periode filters ──────────────────────────────────────────
    f_col1, f_col2, f_col3 = st.columns([1, 1, 2])

    available_ports: list[str] = sorted(frame["Model portfolio"].unique().to_list())
    with f_col1:
        sel_port: str = st.selectbox(
            "Alpha portefeuille",
            options=available_ports,
            key="tx_portfolio",
        )

    min_date: date = frame["Trade date"].min()  # type: ignore[assignment]
    max_date: date = frame["Trade date"].max()  # type: ignore[assignment]
    default_from: date = max(min_date, max_date - timedelta(days=6))

    with f_col2:
        d_from, d_to = st.date_input(
            "Periode (trade date)",
            value=(default_from, max_date),
            min_value=min_date,
            max_value=max_date,
            key="tx_period",
        )

    # ── AUM history lookup ───────────────────────────────────────────────────
    aum_history = store.aum_history_for_model_portfolio(sel_port)

    with f_col3:
        if aum_history:
            latest_aum_date, latest_aum_val = aum_history[-1]
            st.metric(
                label=f"AUM {sel_port}",
                value=_fmt_eur(latest_aum_val),
                help=f"Meest recente waarde per {latest_aum_date.strftime('%d %b %Y')} "
                f"(uit Factor Attributie rapport). "
                f"Bps wordt berekend op basis van de AUM op de handelsdatum.",
            )
        else:
            st.warning(
                f"Geen AUM beschikbaar voor **{sel_port}** in de silver-laag. "
                "Upload eerst een Factor Attributie rapport voor dit portfolio."
            )

    # ── filter en bps berekening ─────────────────────────────────────────────
    df_f = frame.filter(
        (pl.col("Model portfolio") == sel_port) & (pl.col("Trade date") >= d_from) & (pl.col("Trade date") <= d_to)
    )

    if df_f.is_empty():
        st.warning("Geen transacties gevonden voor de geselecteerde periode en portefeuille.")
        return

    has_bps = bool(aum_history)
    if has_bps:
        df_f = _add_bps_column(df_f, aum_history)

    # ── bps schuifbalk (filtert grafiek én table) ─────────────────────────────
    df_vis = df_f  # standaard: geen extra bps-filter

    if has_bps:
        bps_vals = df_f["bps"].drop_nulls()
        if not bps_vals.is_empty():
            bps_lo = float(bps_vals.min())  # type: ignore[arg-type]
            bps_hi = float(bps_vals.max())  # type: ignore[arg-type]
            if bps_lo < bps_hi:
                # Round the lower bound down and the upper bound up (never to
                # nearest) so the widget bounds always fully contain the true
                # data range — a plain round() can round the true max down,
                # silently clipping the largest trade out of the filter by
                # default. A fixed, fine step (rather than one derived from
                # dividing the range into 200 buckets and rounding that to 4
                # decimals) avoids the float drift that made the slider
                # re-snap the untouched handle to a slightly lower value
                # whenever the other handle was dragged.
                bps_lo_r = math.floor(bps_lo * 1000) / 1000
                bps_hi_r = math.ceil(bps_hi * 1000) / 1000
                bps_step = 0.001

                # Reset the selected range whenever the underlying bps bounds change
                # (different portfolio/period selected). Without this, a stale range
                # left over from the previous bounds gets silently clamped by the
                # slider widget, which can quietly drop the new maximum.
                _bounds_key = (sel_port, d_from, d_to)
                if st.session_state.get("_tx_bps_bounds") != _bounds_key:
                    st.session_state["_tx_bps_bounds"] = _bounds_key
                    st.session_state["tx_bps_range"] = (bps_lo_r, bps_hi_r)
                    st.session_state["tx_bps_lo_input"] = bps_lo_r
                    st.session_state["tx_bps_hi_input"] = bps_hi_r

                def _sync_inputs_from_slider() -> None:
                    _lo, _hi = st.session_state["tx_bps_range"]
                    st.session_state["tx_bps_lo_input"] = _lo
                    st.session_state["tx_bps_hi_input"] = _hi

                def _sync_slider_from_inputs() -> None:
                    _lo = min(st.session_state["tx_bps_lo_input"], st.session_state["tx_bps_hi_input"])
                    _hi = max(st.session_state["tx_bps_lo_input"], st.session_state["tx_bps_hi_input"])
                    st.session_state["tx_bps_range"] = (_lo, _hi)

                st.slider(
                    "Filter transacties op bps",
                    min_value=bps_lo_r,
                    max_value=bps_hi_r,
                    step=bps_step,
                    key="tx_bps_range",
                    on_change=_sync_inputs_from_slider,
                    help="Filtert zowel de grafiek als de table op bps-impact per transactie.",
                )

                # Manual bounds live inside a form so typing in either field only
                # triggers one app rerun on "Toepassen" — not one per keystroke/blur
                # per field. Every rerun re-enters the async data-lake read path, so
                # keeping the rerun count low matters on a slow/unstable connection.
                with st.form("tx_bps_manual_form", border=False):
                    in_col1, in_col2, in_col3 = st.columns([1, 1, 1])
                    with in_col1:
                        st.number_input(
                            "Van (bps)",
                            min_value=bps_lo_r,
                            max_value=bps_hi_r,
                            step=bps_step,
                            format="%.3f",
                            key="tx_bps_lo_input",
                            help="Handmatige ondergrens.",
                        )
                    with in_col2:
                        st.number_input(
                            "Tot (bps)",
                            min_value=bps_lo_r,
                            max_value=bps_hi_r,
                            step=bps_step,
                            format="%.3f",
                            key="tx_bps_hi_input",
                            help="Handmatige bovengrens.",
                        )
                    with in_col3:
                        st.markdown("<div style='height:1.9rem'></div>", unsafe_allow_html=True)
                        # on_click runs before the rerun starts (like on_change), so
                        # this may still write to the slider's session-state key here —
                        # doing it after the slider has already been drawn this run
                        # would raise a StreamlitAPIException.
                        st.form_submit_button("Toepassen", on_click=_sync_slider_from_inputs)

                # Round the widget's reported bounds back to the display precision
                # before filtering, so leftover sub-thousandth float noise from the
                # slider can never exclude a trade sitting right at the boundary.
                sel_lo, sel_hi = st.session_state["tx_bps_range"]
                sel_lo, sel_hi = round(sel_lo, 3), round(sel_hi, 3)
                df_vis = df_f.filter((pl.col("bps") >= sel_lo) & (pl.col("bps") <= sel_hi))

    # ── samenvatting ─────────────────────────────────────────────────────────
    m1, m2, m3, m4 = st.columns(4)
    n_buy: int = df_vis.filter(pl.col("Direction") == "Buy").height
    n_sell: int = df_vis.filter(pl.col("Direction") == "Sell").height

    if has_bps:
        total_buy_bps = df_vis.filter(pl.col("Direction") == "Buy")["bps"].sum() or 0.0
        total_sell_bps = df_vis.filter(pl.col("Direction") == "Sell")["bps"].sum() or 0.0
        m1.metric("Aankopen", f"{total_buy_bps:.1f} bps", f"{n_buy} orders")
        m2.metric("Verkopen", f"{total_sell_bps:.1f} bps", f"{n_sell} orders")
        m3.metric("Netto omzet", f"{total_buy_bps - total_sell_bps:+.1f} bps")
    else:
        total_buy_eur = df_vis.filter(pl.col("Direction") == "Buy")["Payment PC"].sum() or 0.0
        total_sell_eur = df_vis.filter(pl.col("Direction") == "Sell")["Payment PC"].sum() or 0.0
        m1.metric("Aankopen (EUR)", f"€{total_buy_eur:,.0f}", f"{n_buy} orders")
        m2.metric("Verkopen (EUR)", f"€{total_sell_eur:,.0f}", f"{n_sell} orders")
        m3.metric("Netto omzet (EUR)", f"€{total_buy_eur - total_sell_eur:+,.0f}")

    m4.metric("Periode", f"{d_from.strftime('%d %b')} – {d_to.strftime('%d %b %Y')}")

    # ── grafiek ───────────────────────────────────────────────────────────────
    if has_bps:
        render_chart(_transaction_bps_chart(df_vis, sel_port))
    else:
        render_chart(_transaction_eur_chart(df_vis, sel_port))

    # ── table ─────────────────────────────────────────────────────────────────
    st.divider()

    with st.expander("Transactiedetails", expanded=True):
        if has_bps:
            st.caption(
                "Bps per transactie is berekend op basis van de AUM op de handelsdatum "
                "(meest recente waarde uit het Factor Attributie rapport op of vóór die datum)."
            )

        display_cols = [
            "Trade date",
            "Security name",
            "Direction",
            "Nominal",
            "Price",
            "Payment PC",
            "bps",
            "Currency",
            "Dealer",
        ]
        cols_present = [col for col in display_cols if col in df_vis.columns]
        st.dataframe(
            df_vis.select(cols_present).sort("Trade date").to_pandas(),
            use_container_width=True,
        )

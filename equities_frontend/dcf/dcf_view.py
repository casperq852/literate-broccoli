"""DCF Model Builder — per-company discounted cash flow valuation from ingested Bloomberg models."""

from __future__ import annotations

from typing import Any, TypedDict

import pandas as pd
import streamlit as st

from dcf import dcf_parsing_model as parsing_model
from dcf import dcf_valuation_model as valuation_model
from dcf.dcf_controller import DcfController
from helpers import equities_config


class _DcfResultState(TypedDict):
    """Session-state shape for a completed DCF run, ready for display and export."""

    result: valuation_model.DcfResult
    net_debt: float
    shares_outstanding: float
    historicals: dict[str, list[parsing_model.ParsedRecord]]
    mapping: dict[str, str | None]
    assumptions: dict[str, dict[str, Any]]


config = equities_config()
dcf_config = config["dcf"]
controller = DcfController()

st.title("DCF Model Builder")
st.caption(
    "Builds a discounted cash flow valuation from a company's ingested Bloomberg "
    "financial model. Only companies with an uploaded MODL_*.xlsx are available below."
)

# ── company selection ──────────────────────────────────────────────────────────

companies = controller.companies_with_financial_model()
if not companies:
    st.info(
        "No companies with an ingested financial model yet. Upload a MODL_*.xlsx "
        "file on the Upload Document page first."
    )
    st.stop()

preselect_ticker = st.session_state.pop("dcf_preselect_ticker", None)
preselect_index = companies.index(preselect_ticker) if preselect_ticker in companies else 0
ticker = st.selectbox("Company (ticker)", options=companies, index=preselect_index)

if st.session_state.get("dcf_ticker") != ticker:
    for key in ("dcf_records", "dcf_mapping_confirmed", "dcf_mapping", "dcf_llm_suggestion", "dcf_result"):
        st.session_state.pop(key, None)
    st.session_state["dcf_ticker"] = ticker

if st.button("Load financial data", type="primary"):
    with st.spinner("Retrieving financial model data..."):
        st.session_state["dcf_records"] = controller.retrieve_records(
            ticker, config["index_name"], dcf_config["max_bulk_records"]
        )
    st.session_state["dcf_mapping_confirmed"] = False
    st.session_state.pop("dcf_result", None)
    st.rerun()

records: list[parsing_model.ParsedRecord] | None = st.session_state.get("dcf_records")
if records is None:
    st.stop()
if not records:
    st.warning("No parseable financial-model records were found for this company.")
    st.stop()
assert records is not None  # narrows for the type checker; st.stop() above already halted otherwise

# ── metric mapping review ─────────────────────────────────────────────────────

st.divider()
st.subheader("Confirm metric mapping")
st.caption(
    "Each line item below was auto-matched from the retrieved Bloomberg model. "
    "Review and change any mapping before generating the DCF."
)

candidates = controller.candidate_mapping(records)
mapping: dict[str, str | None] = {}
missing_required: list[str] = []

for item_id, item_rule in controller.canonical_line_items.items():
    item_candidates = candidates.get(item_id, [])
    options = ["— None / not available —", *item_candidates]
    label = item_rule["label"] + (" *" if item_rule["required"] else "")
    choice = st.selectbox(label, options=options, index=1 if item_candidates else 0, key=f"dcf_map_{item_id}")
    chosen = choice if choice != options[0] else None
    mapping[item_id] = chosen

    if chosen:
        preview = parsing_model.time_series_for_metric(records, chosen)[-3:]
        if preview:
            preview_text = ", ".join(f"{record.period_end_date[:10]}: {record.value:,.1f}" for record in preview)
            st.caption(f"Preview (most recent): {preview_text}")
    if item_rule["required"] and chosen is None:
        missing_required.append(item_rule["label"])

if missing_required:
    st.warning(f"Required line items not mapped: {', '.join(missing_required)}.")

if st.button("Confirm mappings and continue", type="primary", disabled=bool(missing_required)):
    st.session_state["dcf_mapping"] = mapping
    st.session_state["dcf_mapping_confirmed"] = True
    st.rerun()

if not st.session_state.get("dcf_mapping_confirmed"):
    st.stop()

confirmed_mapping: dict[str, str | None] = st.session_state["dcf_mapping"]
historicals = controller.historical_series(records, confirmed_mapping)

# ── historical-trend defaults ─────────────────────────────────────────────────

revenue_series = historicals.get("revenue", [])
base_revenue = parsing_model.latest_value(revenue_series) or parsing_model.latest_value(
    revenue_series, reported_only=False
)
if base_revenue is None:
    st.error("Could not find any revenue value (reported or estimated) to use as the DCF's starting point.")
    st.stop()
assert base_revenue is not None  # narrows for the type checker; st.stop() above already halted otherwise

hist_growth = parsing_model.average_growth_rate(revenue_series)

ebit_series = historicals.get("ebit", [])
ebitda_series = historicals.get("ebitda", [])
d_and_a_hist_series = historicals.get("d_and_a", [])
margin_note = ""
if ebit_series:
    effective_ebit_series = ebit_series
elif ebitda_series and d_and_a_hist_series:
    effective_ebit_series = parsing_model.derive_difference_series(ebitda_series, d_and_a_hist_series)
    margin_note = "EBIT derived as EBITDA minus D&A (an explicit EBIT line item was not found)."
elif ebitda_series:
    effective_ebit_series = ebitda_series
    margin_note = "Using EBITDA as an EBIT proxy — EBIT was not found; margins may be overstated."
else:
    effective_ebit_series = []
    margin_note = "No EBIT or EBITDA line found — set the margin assumption manually below."
if margin_note:
    st.warning(margin_note)
hist_margin = parsing_model.average_margin(effective_ebit_series, revenue_series) if effective_ebit_series else None

net_debt_series = historicals.get("net_debt", [])
total_debt_series = historicals.get("total_debt", [])
cash_series = historicals.get("cash_and_equivalents", [])
net_debt_note = ""
if net_debt_series:
    effective_net_debt_series = net_debt_series
elif total_debt_series and cash_series:
    effective_net_debt_series = parsing_model.derive_difference_series(total_debt_series, cash_series)
    net_debt_note = "Net debt derived as Total Debt minus Cash & Equivalents (Net Debt line item not found)."
else:
    effective_net_debt_series = []
    net_debt_note = "Net debt not found in the model — defaulting to 0. Verify this before trusting the valuation."
if net_debt_note:
    st.warning(net_debt_note)
default_net_debt: float | None = parsing_model.latest_value(effective_net_debt_series)
if default_net_debt is None:
    # Falls back to an estimated period, then to 0, without using an `or` chain -- net debt can
    # legitimately be exactly 0 (cash-rich companies), and `or` would treat a real 0 as missing.
    default_net_debt = parsing_model.latest_value(effective_net_debt_series, reported_only=False)
if default_net_debt is None:
    default_net_debt = 0.0

shares_series = historicals.get("shares_outstanding", [])
default_shares = parsing_model.latest_value(shares_series) or parsing_model.latest_value(
    shares_series, reported_only=False
)

d_and_a_pct_default = (
    parsing_model.average_margin(d_and_a_hist_series, revenue_series) if d_and_a_hist_series else 0.0
) or 0.0
capex_series = historicals.get("capex", [])
capex_pct_default = (parsing_model.average_margin(capex_series, revenue_series) if capex_series else 0.0) or 0.0

# ── LLM-assisted suggestion ────────────────────────────────────────────────────

st.divider()
st.subheader("Starting assumptions")

with st.expander("LLM-suggested starting assumptions (from research commentary)", expanded=False):
    if st.button("Get LLM suggestion", key="fetch_llm_suggestion"):
        with st.spinner("Reading research commentary..."):
            st.session_state["dcf_llm_suggestion"] = controller.suggest_assumptions_from_research(
                company=ticker, ticker=ticker, config=config
            )
    llm_suggestion = st.session_state.get("dcf_llm_suggestion")
    if llm_suggestion is None:
        st.caption("Not fetched yet.")
    elif not llm_suggestion.available:
        st.caption("No usable suggestion (no research found, or the response could not be parsed).")
    else:
        st.markdown(
            f"Growth: **{llm_suggestion.revenue_growth_rate:.1%}** · "
            f"EBIT margin: **{llm_suggestion.ebit_margin:.1%}** · "
            f"Discount rate: **{llm_suggestion.discount_rate:.1%}**"
        )
        st.caption(llm_suggestion.rationale)

llm_suggestion = st.session_state.get("dcf_llm_suggestion")

# ── assumption inputs (session-state-gated so a typed value is never silently overwritten) ──


def _default_if_absent(key: str, fallback: float) -> None:
    if key not in st.session_state:
        st.session_state[key] = fallback


_default_if_absent("dcf_growth_input", hist_growth if hist_growth is not None else 0.05)
_default_if_absent("dcf_margin_input", hist_margin if hist_margin is not None else 0.15)
_default_if_absent("dcf_discount_input", dcf_config["default_discount_rate"])

col_growth, col_growth_hist, col_growth_llm = st.columns([2, 1, 1])
with col_growth_hist:
    if hist_growth is not None and st.button("Use historical avg", key="use_hist_growth"):
        st.session_state["dcf_growth_input"] = round(hist_growth, 4)
        st.rerun()
with col_growth_llm:
    if llm_suggestion and llm_suggestion.available and st.button("Use LLM suggestion", key="use_llm_growth"):
        st.session_state["dcf_growth_input"] = round(llm_suggestion.revenue_growth_rate, 4)
        st.rerun()
with col_growth:
    growth_rate = st.slider(
        "Revenue growth rate (year 1)", min_value=-0.50, max_value=1.00, step=0.01, key="dcf_growth_input"
    )

col_margin, col_margin_hist, col_margin_llm = st.columns([2, 1, 1])
with col_margin_hist:
    if hist_margin is not None and st.button("Use historical avg", key="use_hist_margin"):
        st.session_state["dcf_margin_input"] = round(hist_margin, 4)
        st.rerun()
with col_margin_llm:
    if llm_suggestion and llm_suggestion.available and st.button("Use LLM suggestion", key="use_llm_margin"):
        st.session_state["dcf_margin_input"] = round(llm_suggestion.ebit_margin, 4)
        st.rerun()
with col_margin:
    ebit_margin = st.slider("EBIT margin", min_value=0.0, max_value=1.0, step=0.01, key="dcf_margin_input")

col_disc, _col_disc_hist, col_disc_llm = st.columns([2, 1, 1])
with col_disc_llm:
    if llm_suggestion and llm_suggestion.available and st.button("Use LLM suggestion", key="use_llm_discount"):
        st.session_state["dcf_discount_input"] = round(llm_suggestion.discount_rate, 4)
        st.rerun()
with col_disc:
    discount_rate = st.slider(
        "Discount rate (WACC)",
        min_value=dcf_config["min_discount_rate"],
        max_value=dcf_config["max_discount_rate"],
        step=0.005,
        key="dcf_discount_input",
    )

terminal_growth_rate = st.slider(
    "Terminal growth rate",
    min_value=dcf_config["min_terminal_growth_rate"],
    max_value=dcf_config["max_terminal_growth_rate"],
    value=dcf_config["default_terminal_growth_rate"],
    step=0.0025,
)
projection_years = st.slider(
    "Projection years",
    min_value=dcf_config["min_projection_years"],
    max_value=dcf_config["max_projection_years"],
    value=dcf_config["default_projection_years"],
)
tax_rate = st.slider("Tax rate", min_value=0.0, max_value=0.5, value=dcf_config["default_tax_rate"], step=0.01)
d_and_a_pct = st.slider("D&A (% of revenue)", min_value=0.0, max_value=0.3, value=float(d_and_a_pct_default), step=0.01)
capex_pct = st.slider("Capex (% of revenue)", min_value=0.0, max_value=0.3, value=float(capex_pct_default), step=0.01)

col_net_debt, col_shares = st.columns(2)
with col_net_debt:
    net_debt_input = st.number_input("Net debt", value=float(default_net_debt))
with col_shares:
    shares_input = st.number_input("Shares outstanding", value=float(default_shares or 0.0), min_value=0.0)

if discount_rate <= terminal_growth_rate:
    st.error("Discount rate must be greater than the terminal growth rate.")
    st.stop()

generate_clicked = st.button("Generate DCF", type="primary", disabled=shares_input <= 0)

if generate_clicked:
    growth_rates = valuation_model.fade_growth_rate(growth_rate, terminal_growth_rate, projection_years)
    ebit_margins = [ebit_margin] * projection_years
    projected_revenue_preview = valuation_model.project_revenue(base_revenue, growth_rates)
    d_and_a_values = valuation_model.apply_percent_of_revenue(projected_revenue_preview, d_and_a_pct)
    capex_values = valuation_model.apply_percent_of_revenue(projected_revenue_preview, capex_pct)
    change_in_nwc = [0.0] * projection_years

    try:
        result = controller.run_valuation(
            base_revenue=base_revenue,
            revenue_growth_rates=growth_rates,
            ebit_margins=ebit_margins,
            tax_rate=tax_rate,
            d_and_a=d_and_a_values,
            capex=capex_values,
            change_in_nwc=change_in_nwc,
            discount_rate=discount_rate,
            terminal_growth_rate=terminal_growth_rate,
            net_debt=net_debt_input,
            shares_outstanding=shares_input,
        )
    except ValueError as exc:
        st.error(f"Could not compute a valuation: {exc}")
    else:
        dcf_result_state: _DcfResultState = {
            "result": result,
            "net_debt": net_debt_input,
            "shares_outstanding": shares_input,
            "historicals": historicals,
            "mapping": confirmed_mapping,
            "assumptions": {
                "revenue_growth_rate (year 1)": {"value": growth_rate, "source": "user-set"},
                "ebit_margin": {"value": ebit_margin, "source": "user-set"},
                "tax_rate": {"value": tax_rate, "source": "user-set"},
                "d_and_a_pct_of_revenue": {"value": d_and_a_pct, "source": "user-set"},
                "capex_pct_of_revenue": {"value": capex_pct, "source": "user-set"},
                "discount_rate": {"value": discount_rate, "source": "user-set"},
                "terminal_growth_rate": {"value": terminal_growth_rate, "source": "user-set"},
                "projection_years": {"value": projection_years, "source": "user-set"},
                "net_debt": {"value": net_debt_input, "source": "user-set"},
                "shares_outstanding": {"value": shares_input, "source": "user-set"},
            },
        }
        st.session_state["dcf_result"] = dcf_result_state
        st.rerun()

# ── results ────────────────────────────────────────────────────────────────────

dcf_result: _DcfResultState | None = st.session_state.get("dcf_result")
if not dcf_result:
    st.stop()
assert dcf_result is not None  # narrows for the type checker; st.stop() above already halted otherwise

result: valuation_model.DcfResult = dcf_result["result"]

st.divider()
st.subheader(f"Valuation — {ticker}")

col_ev, col_eq, col_share = st.columns(3)
col_ev.metric("Enterprise value", f"{result.enterprise_value:,.0f}")
col_eq.metric("Equity value", f"{result.equity_value:,.0f}")
col_share.metric("Value per share", f"{result.value_per_share:,.2f}")

projection_table = pd.DataFrame(
    {
        "Year": [f"Year {i + 1}" for i in range(len(result.projected_revenue))],
        "Revenue": result.projected_revenue,
        "EBIT": result.projected_ebit,
        "NOPAT": result.projected_nopat,
        "Unlevered FCF": result.projected_fcf,
        "Discount factor": result.discount_factors,
        "PV of FCF": result.pv_fcf,
    }
)
st.dataframe(projection_table, use_container_width=True, hide_index=True)
st.caption(f"Terminal value: {result.terminal_value:,.0f}  ·  PV of terminal value: {result.pv_terminal_value:,.0f}")

st.divider()
llm_suggestion = st.session_state.get("dcf_llm_suggestion")
rationale = llm_suggestion.rationale if llm_suggestion and llm_suggestion.available else ""
workbook_bytes = controller.build_workbook(
    company=ticker,
    ticker=ticker,
    mapping=dcf_result["mapping"],
    historicals=dcf_result["historicals"],
    line_item_labels={item_id: rule["label"] for item_id, rule in controller.canonical_line_items.items()},
    assumptions=dcf_result["assumptions"],
    result=result,
    net_debt=dcf_result["net_debt"],
    shares_outstanding=dcf_result["shares_outstanding"],
    rationale=rationale,
)
st.download_button(
    label="Download DCF Workbook (Excel)",
    data=workbook_bytes,
    file_name=f"dcf_{ticker.lower()}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

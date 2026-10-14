# Frontend pages

The Streamlit app presents a top navigation bar with six pages, plus an admin-only page that appears for administrators. Two further views, the company detail and the DCF builder, are reached in context rather than from the navigation bar. Every page follows the Model View Controller split: the `*_view.py` renders widgets, the `*_controller.py` supplies data, and the `*_model.py` performs the calculations.

## Navigation pages

| Page | View file | Purpose |
|------|-----------|---------|
| Chat | `chat/chat_view.py` | History-aware question answering over the research index. |
| Investment Case Builder | `investment_case/investment_case_view.py` | Drafts a structured investment case from indexed research. |
| Portfolio Dashboard | `portfolio_dashboard_view.py` | The colleague's Bloomberg portfolio-analytics dashboard, vendored in as its own page. |
| Index Browser | `index/index_view.py` | Browses and syncs the documents present in the index. |
| Upload Document | `upload/upload_view.py` | Uploads a single document and ingests it. |
| Bulk Upload (zip) | `upload/bulk_upload_view.py` | Uploads and ingests many documents from an archive. |
| User Management | `admin/users_view.py` | Admin-only create, delete, and password reset for users. |

## Chat

The chat page answers questions against the index and keeps a running conversation. It condenses each follow-up into a standalone query, retrieves the most relevant chunks, generates a grounded answer, and lists the sources. Predefined prompts come from `quick_questions.yaml`. Conversations are saved per tab to the Data Lake so an analyst can resume earlier sessions; the retrieval and answering mechanics are described in [retrieval-and-chat](retrieval-and-chat).

If a question names a known portfolio (e.g. "What's the tracking error on ADAAF?"), `chat_controller.ChatController._portfolio_context_chunk` looks up that portfolio's latest cached metrics (`database.cache.get_portfolio_metrics`, defaulting to the most recent date) and grounds the answer in it directly — a structured lookup, not retrieval, since exact portfolio numbers need deterministic answers. See [Portfolio Dashboard](#portfolio-dashboard) below for where that data comes from.

## Investment Case Builder

The investment case builder assembles a multi-section case (for example the business, the moat, and the risks) by retrieving research for the chosen company and prompting the model per section. `investment_case_controller.InvestmentCaseController` drives the section list, source grouping, and generation, while `investment_case_model` handles the prompt construction and the Markdown to HTML rendering. It can also plan and generate a narrated dialogue between analyst personas defined in `investment_case_content.yaml`.

The "Role in the Portfolio" section is additionally grounded in the company's real positions: `InvestmentCaseController._portfolio_positioning_chunk` checks every known portfolio's latest cached holdings for a fuzzy name match and, if held, reports its weight and risk contribution per portfolio — the reverse lookup of Chat's portfolio-name lookup above.

## Portfolio Dashboard

This page hosts a colleague's independent Bloomberg portfolio-analytics dashboard — attribution, tracking error/risk, sector and factor attribution, and factor-level risk exposures across seven tabs — vendored in wholesale and unmodified under `portfolio_dashboard/`. `portfolio_dashboard_view.py` is a thin wrapper that constructs their own `DashboardController` and calls `.run()`; their sidebar and tabs render underneath this app's top navigation banner. The vendored package keeps its own internal structure and dependencies (including `pandas`, at its Plotly-rendering boundary) exactly as delivered — new code elsewhere in this app uses `polars` instead.

The hourly scheduler tick (`scheduler_controller._run_portfolio_sync`) calls the colleague's own `MedallionRefresher.refresh()` and `SilverStore.load_reports_by_portfolio()` unmodified, then summarizes each portfolio's full holdings, risk contributors, and factor exposures — not just a top-N slice, since an alpha portfolio only holds ~40-60 names — into this app's own `portfolio_metrics` SQLite table (`database/cache.py`) via `portfolio_dashboard.chat_context_model.summarize_portfolio`. That table is automatically included in the existing whole-database Data Lake backup, and is what Chat and the Investment Case Builder query directly (see above) rather than re-reading the silver layer live.

## Index Browser

The index browser lists the documents that are currently indexed, using the local SQLite cache for fast display, and can synchronise that cache from Azure AI Search. `index_controller.IndexController` exposes `load_documents` and `sync_from_azure` for the view.

## Upload Document and Bulk Upload

The single upload page accepts one file whose extension is in `allowed_extensions`, writes it to the Data Lake, and runs the ingestion pipeline on it. `upload_controller.UploadController` validates the extension, uploads the file, and calls the ingestion path. The bulk page accepts a `zip` archive, lists the supported members, uploads them, and ingests them with progress reporting. Both pages reuse the same ingestion entry point as the standalone pipeline.

## User Management

Visible only when `is_admin` is set, this page manages the accounts stored in `auth_config.yaml`. `auth_controller.UsersController` supports creating users, deleting users, and resetting passwords, all hashed with `bcrypt`.

## Contextual views

Two views are reached from within the app rather than from the navigation bar.

- **Company detail** (`companies/company_detail_view.py`) is rendered by `companies/company_view.py` when a `ticker` query parameter is present. It shows the company summary, its indexed documents with optional inline PDF preview, and a financial summary drawn from any Bloomberg model records.
- **DCF model builder** (`dcf/dcf_view.py`) is opened from the company detail view. It maps model line items to canonical items, builds a historical series, optionally suggests assumptions from research, runs a two-stage discounted cash flow valuation, and exports the result to an Excel workbook. The valuation math lives in `dcf_valuation_model` and the workbook export in `dcf_export_model`.

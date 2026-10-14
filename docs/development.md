# Development

This page describes the coding standards and the local tooling used when changing the codebase.

## Coding standards

The code follows the a.s.r. Vermogensbeheer Python standard: a topic-based Model View Controller structure with PEP 8 styling, full type annotations, and Google-style docstrings. Each topic folder holds one controller alongside its models, views, and helpers.

| Component | Responsibility |
|-----------|----------------|
| Model | Pure calculations and data transforms on minimal input types. No dataset knowledge. |
| View | Visualisations and Streamlit widgets. May read from a model. |
| Controller | Wires dataset-specific data into models and views. Slices and filters, never calculates. |
| Helper | Technical utilities such as file reads and Data Lake access. |

The review checklist is short: a model never imports a view or controller, a view never imports a controller, a controller performs no calculations, and no value that belongs in configuration is hard-coded. New behaviour that needs a constant, a path, or a threshold adds it to the relevant file under `configs/` rather than inline.

New code in this repo prefers `polars` over `pandas` for any tabular work. The one exception is the vendored Portfolio Dashboard package (`equities_frontend/portfolio_dashboard/`, see [architecture](architecture)), which keeps using `pandas` at its own Plotly-rendering boundary by its own, unmodified design.

## Tooling

Formatting, linting, typing, and spelling are enforced by pre-commit hooks configured in `.pre-commit-config.yaml`, with tool settings in `pyproject.toml`.

| Tool | Role | Notes |
|------|------|-------|
| `black` | Formatter | Line length `122`. Runs on Python files only. |
| `ruff` | Linter | Line length `122`. Runs on Python files only. |
| `ty` | Type checker | Static type analysis over the package. |
| `codespell` | Spell checker | Runs on documentation and code; a Dutch and domain word allow-list avoids false positives. |
| `pre-commit` | Hook runner | Orchestrates the above on staged files. |

Install and run the hooks locally:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

Because the hooks only target Python files, the `Dockerfile`, `requirements.txt`, YAML configuration, and Markdown documentation are excluded from `black`, `ruff`, and `ty`. Documentation is still spell-checked by `codespell`.

## Running components standalone

Two parts of the system can be exercised without the full app, which is useful when developing ingestion features:

- The vectorization pipeline runs as `python -m vectorization` with the arguments described in [vectorization-pipeline](vectorization-pipeline).
- A single Bloomberg model can be parsed and inspected with `python -m vectorization.financial_model.cli --file <path> --show-paths`.

The Streamlit app itself is started with `python equities_frontend/main.py --port 8501` as covered in [getting-started](getting-started).

## Changelog

User-visible and structural changes are recorded in `CHANGELOG.md` at the repository root, which is the anchor for the project history. Add an entry there when a change alters behaviour, configuration, or the module layout.

"""Package entry point so the dashboard can be started with:

    python -m portfolio_dashboard

Delegates to :func:`portfolio_dashboard.main.main`, the same launcher used by
``python portfolio_dashboard/main.py``.
"""

from portfolio_dashboard.main import main

if __name__ == "__main__":
    main()

"""CLI entry point that launches the Portfolio Analytics Streamlit dashboard.

Run the dashboard with:

    python portfolio_dashboard/main.py [--port PORT]

The launcher starts ``app.py`` through the Streamlit CLI as a subprocess, mirroring
the shared house-style entry point used by the other a.s.r. dashboards.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from argparse import ArgumentParser, Namespace
from logging import Logger
from pathlib import Path
from subprocess import Popen

from dotenv import load_dotenv
from streamlit.logger import get_logger

logger: Logger = get_logger(name=__name__)

logger.debug(msg="Initializing CLI entrypoint...")

load_dotenv()

APP_PORT: str = os.getenv("APP_PORT", "8501")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "info").lower()

os.environ["LOG_LEVEL"] = LOG_LEVEL.upper()

# Give matplotlib (pulled in transitively by the dashboard) a writable cache dir
# on the read-only a.s.r. Azure ML $HOME so the launched Streamlit subprocess
# never warns; the subprocess inherits this via ``env=os.environ`` below.
if not os.environ.get("MPLCONFIGDIR"):
    os.environ["MPLCONFIGDIR"] = str(Path(tempfile.gettempdir()) / "portfolio_dashboard_mpl")
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)


def init_parser() -> ArgumentParser:
    """Initialize the argument parser for the Streamlit launcher.

    Returns:
        The configured argument parser, exposing an optional ``--port`` argument
        that defaults to the ``APP_PORT`` environment variable (or ``8501``).
    """
    logger.debug(msg="Initializing argument parser...")
    parser: ArgumentParser = ArgumentParser(description="Streamlit launcher for the Portfolio Analytics dashboard.")
    parser.add_argument(
        "--port",
        default=APP_PORT,
        help="Port to run the Streamlit app on (default from env or 8501)",
    )
    return parser


def main() -> None:
    """Launch the Streamlit dashboard.

    Parses CLI arguments, resolves the absolute path to ``app.py``, and starts it
    through the Streamlit CLI as a subprocess. Streamlit output is forwarded and the
    process is terminated gracefully on ``KeyboardInterrupt``.

    Raises:
        FileNotFoundError: If ``app.py`` cannot be found next to this launcher.
        Exception: If the Streamlit subprocess fails to start.
    """
    parser: ArgumentParser = init_parser()
    args: Namespace = parser.parse_args()
    logger.debug(msg=f"Parsed CLI args: port={args.port}")

    current_dir: Path = Path(__file__).resolve().parent
    app_path: Path = current_dir / "app.py"
    logger.debug(msg=f"Resolved absolute app path: {app_path}")

    if not app_path.exists():
        logger.error(msg=f"App file does not exist at: {app_path}")
        raise FileNotFoundError(f"Streamlit app not found at: {app_path}")

    logger.info(msg=f"Launching Streamlit app on port {args.port}")
    logger.debug(msg=f"Running command: {sys.executable} -m streamlit run {app_path} --server.port {args.port}")

    try:
        process: Popen[str] = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(app_path),
                "--server.port",
                str(args.port),
                "--theme.base",
                "light",
            ],
            text=True,
            env=os.environ,
        )
        app_url: str = f"http://localhost:{args.port}"
        logger.info(msg=f"Streamlit app launched at {app_url}. Press Ctrl+C to stop.")

        try:
            stdout, stderr = process.communicate()
            if stdout:
                logger.debug(msg=f"Streamlit STDOUT:\n{stdout}")
            if stderr:
                logger.warning(msg=f"Streamlit STDERR:\n{stderr}")
            if process.returncode != 0:
                logger.error(msg="Streamlit app exited with a non-zero status.")
        except KeyboardInterrupt:
            logger.info(msg="Terminating Streamlit app...")
            process.terminate()
            process.wait()

    except Exception as exc:
        logger.exception(msg=f"Failed to launch Streamlit app: {exc}")
        raise


if __name__ == "__main__":
    main()

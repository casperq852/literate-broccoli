import contextlib
import os
import subprocess
from argparse import ArgumentParser, Namespace
from logging import Logger
from subprocess import Popen

from dotenv import load_dotenv
from streamlit.logger import get_logger

logger: Logger = get_logger(name=__name__)

logger.debug(msg="Initializing CLI entrypoint...")

load_dotenv()


ACCOUNT_NAME: str | None = os.getenv("ACCOUNT_NAME")
CLIENT_ID: str | None = os.getenv("DEFAULT_IDENTITY_CLIENT_ID")
APP_PORT: str = os.getenv("APP_PORT", "8501")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "debug").lower()

os.environ["LOG_LEVEL"] = LOG_LEVEL.upper()

# Matplotlib's default config dir (~/.config/matplotlib) isn't guaranteed
# writable on every host this runs on (seen failing outright on at least one
# Azure VM user) -- its own fallback is then a *new random temp dir every
# process start*, so the font cache it builds on first import never survives
# a restart anywhere that default isn't writable. Pointing it at a fixed,
# always-writable path once here means the cache is actually reused across
# restarts on any host. Only a default (setdefault, not an override) so an
# explicit MPLCONFIGDIR from .env or the Docker image (which pre-builds this
# cache at image build time, see Dockerfile) always wins.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/equities_rag/matplotlib_cache")  # noqa: S108
# non-fatal -- matplotlib falls back to its own temp dir if this still isn't writable
with contextlib.suppress(OSError):
    os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

# Console verbosity of Streamlit's own loggers (uvicorn/server/watcher spam).
# Kept at "warning" so the launcher output stays clean; override via env if needed.
STREAMLIT_LOG_LEVEL: str = os.getenv("STREAMLIT_LOG_LEVEL", "warning").lower()


def init_parser() -> ArgumentParser:
    """
    Initializes and returns an argument parser for the Streamlit launcher.

    The parser includes an optional '--port' argument to specify the port on which
    the Streamlit app will run. If not provided, it defaults to the value of APP_PORT.

    Returns:
        ArgumentParser: Configured argument parser for the application.
    """
    logger.debug(msg="Initializing argument parser...")
    parser: ArgumentParser = ArgumentParser(description="Streamlit launcher for version_selector.")
    parser.add_argument(
        "--port",
        default=APP_PORT,
        help="Port to run the Streamlit app on (default from env or 8501)",
    )
    return parser


def main() -> None:
    """
    Main entry point for launching the Streamlit application.

    This function initializes the command-line argument parser, parses the arguments,
    and attempts to launch the Streamlit app (`version_selector.py`) with the specified
    port and theme options. It performs the following steps:

    1. Initializes and parses CLI arguments.
    2. Resolves the absolute path to the Streamlit app file.
    3. Checks for the existence of the app file and raises an error if not found.
    4. Launches the Streamlit app as a subprocess with the specified port and theme.
    5. Logs relevant information and handles subprocess output.
    6. Gracefully handles termination via KeyboardInterrupt.
    7. Logs and raises exceptions if the app fails to launch.

    Raises:
        FileNotFoundError: If the Streamlit app file does not exist.
        Exception: For any other errors encountered during app launch.
    """
    parser: ArgumentParser = init_parser()
    args: Namespace = parser.parse_args()
    logger.debug(msg=f"Parsed CLI args: port={args.port}")

    current_dir: str = os.path.dirname(os.path.abspath(__file__))
    app_path: str = os.path.join(current_dir, "version_selector.py")
    logger.debug(msg=f"Resolved absolute app path: {app_path}")

    if not os.path.exists(path=app_path):
        logger.error(msg=f"App file does not exist at: {app_path}")
        raise FileNotFoundError(f"Streamlit app not found at: {app_path}")

    if os.getenv("EQUITIES_SCHEDULER_ENABLED", "true").lower() != "false":
        try:
            from scheduler.scheduler_controller import start_scheduler_thread  # noqa: PLC0415

            logger.debug(msg="Starting background scheduler thread...")
            start_scheduler_thread()
        except Exception:  # noqa: BLE001
            # A failure importing or starting the scheduler (e.g. a bad config
            # path) must never take the whole container down before Streamlit
            # even launches -- the app itself works without it, just without
            # hourly classification/portfolio-sync/backup until this is fixed.
            logger.exception(msg="Background scheduler failed to start; continuing without it")

    logger.info(msg=f"Launching Streamlit app on port {args.port}")
    logger.debug(msg=f"Running command: streamlit run {app_path} --server.port {args.port}")

    try:
        process: Popen[str] = subprocess.Popen(  # noqa: S603 - fixed args, trusted launcher
            [  # noqa: S607 - launching streamlit from PATH is intended
                "streamlit",
                "run",
                app_path,
                "--server.port",
                args.port,
                "--theme.base",
                "light",
                f"--logger.level={STREAMLIT_LOG_LEVEL}",
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
            try:
                process.wait(timeout=5)
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                process.kill()

    except Exception as e:
        logger.exception(msg=f"Failed to launch Streamlit app: {e}")
        raise


if __name__ == "__main__":
    main()

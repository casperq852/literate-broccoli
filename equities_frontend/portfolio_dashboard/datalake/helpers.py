"""Pure helpers for the Data Lake medallion pipeline.

These functions carry no I/O: they parse landing-zone file names, derive the
``EQT/portfolio/<code>/YYYY/MM/DD`` medallion paths shared by the bronze and silver
layers,
normalise portfolio names into path-safe slugs and classify report objects into a
stable storage key. Keeping them pure makes the pipeline easy to type-check and
unit-test in isolation (a.s.r. coding standard: Model layer, no side effects).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from portfolio_dashboard.config import load_app_config

# The report-object class name maps to a short, stable key used in silver file
# names. Sourced from the class name so the mapping needs no runtime imports.
_REPORT_KEY_BY_CLASS: dict[str, str] = {
    "AttributionReport": "attr",
    "TrackingErrorReport": "te",
    "SectorAttributionReport": "sector",
    "FactorAttributionReport": "factor",
    "TERiskFactorReport": "te_factors",
}


@dataclass(frozen=True)
class LandingFile:
    """A parsed landing-zone data-file name.

    Attributes:
        filename: The original file name, unchanged.
        code: The fund family code, for example ``ADAAF``.
        token: The report token between the code and the data suffix.
        date: The eight-digit ``YYYYMMDD`` run date taken from the file name.
        ext: The lower-case file extension without a dot, for example ``csv``.
    """

    filename: str
    code: str
    token: str
    date: str
    ext: str

    @property
    def year(self) -> str:
        """Return the four-digit year portion of the run date."""
        return self.date[0:4]

    @property
    def month(self) -> str:
        """Return the two-digit month portion of the run date."""
        return self.date[4:6]

    @property
    def day(self) -> str:
        """Return the two-digit day portion of the run date."""
        return self.date[6:8]

    @property
    def stem(self) -> str:
        """Return the file name without its extension."""
        return self.filename[: -(len(self.ext) + 1)]


def _datalake_config() -> dict:
    """Return the ``datalake`` section of the central application config."""
    return load_app_config()["datalake"]


def _filename_pattern() -> re.Pattern[str]:
    """Build the landing-zone file-name regex from configuration.

    Returns:
        re.Pattern[str]: A compiled pattern with ``code``, ``token``, ``date`` and
        ``ext`` groups.
    """
    config = _datalake_config()
    data_suffix = re.escape(str(config["data_suffix"]))
    extensions = "|".join(re.escape(str(ext)) for ext in config["data_extensions"])
    return re.compile(rf"^(?P<code>[A-Za-z0-9]+)_(?P<token>.+)_{data_suffix}_(?P<date>\d{{8}})\.(?P<ext>{extensions})$")


def parse_landing_filename(name: str) -> LandingFile | None:
    """Parse a landing-zone data-file name into its components.

    Args:
        name: A bare file name (no directory), for example
            ``ADAAF_Fonds_TE_report_EQT100_20260720.csv``.

    Returns:
        LandingFile | None: The parsed parts, or ``None`` when ``name`` is not a
        recognised data file (status files and other names return ``None``).
    """
    match = _filename_pattern().match(name)
    if match is None:
        return None
    return LandingFile(
        filename=name,
        code=match.group("code").upper(),
        token=match.group("token"),
        date=match.group("date"),
        ext=match.group("ext").lower(),
    )


def status_filename_for(landing: LandingFile) -> str:
    """Return the sibling status-file name for a landing data file.

    Args:
        landing: The parsed data file.

    Returns:
        str: The status file name that would accompany ``landing``.
    """
    return f"{landing.stem}{_datalake_config()['status_suffix']}"


def is_status_success(content: str) -> bool:
    """Report whether a status-file body signals a successful generation.

    Args:
        content: The decoded text content of a ``.status.txt`` file.

    Returns:
        bool: ``True`` when the configured success marker appears in ``content``.
    """
    marker = str(_datalake_config()["status_success_marker"]).lower()
    return marker in content.lower()


def date_to_iso(date: str) -> str:
    """Convert an eight-digit ``YYYYMMDD`` date into ``YYYY-MM-DD``.

    Args:
        date: An eight-digit date string.

    Returns:
        str: The ISO-formatted date.
    """
    return f"{date[0:4]}-{date[4:6]}-{date[6:8]}"


def portfolio_slug(portfolio: str) -> str:
    """Normalise a portfolio name into an upper-case, path-safe slug.

    Args:
        portfolio: A report ``metadata.portfolio`` value such as
            ``"EQUS ALPHA_ADAAF"``.

    Returns:
        str: A slug with runs of non-alphanumeric characters collapsed to single
        underscores, for example ``"EQUS_ALPHA_ADAAF"``.
    """
    slug = re.sub(r"[^0-9A-Za-z]+", "_", portfolio.strip().upper())
    return slug.strip("_")


def report_storage_key(report: object) -> str:
    """Return the stable storage key for a parsed report object.

    Args:
        report: Any of the report dataclasses produced by the parser.

    Returns:
        str: A short key such as ``"attr"`` or ``"te_factors"``.

    Raises:
        KeyError: When the report type has no configured storage key.
    """
    return _REPORT_KEY_BY_CLASS[type(report).__name__]


def _medallion_dir(code: str, landing: LandingFile) -> str:
    """Return the shared bronze/silver directory for a landing file.

    The bronze and silver layers nest promoted files under the configured
    sub-folder, producing ``EQT/portfolio/<code>/YYYY/MM/DD``.

    Args:
        code: The fund family code, for example ``ADAAF``.
        landing: The parsed landing data file supplying the date folders.

    Returns:
        str: The shared directory path for the bronze and silver layers.
    """
    config = _datalake_config()
    root = str(config["root"])
    subfolder = str(config["bronze_silver_subfolder"])
    return f"{root}/{subfolder}/{code}/{landing.year}/{landing.month}/{landing.day}"


def build_bronze_path(landing: LandingFile) -> str:
    """Return the bronze-layer path for a landing data file.

    Args:
        landing: The parsed landing-zone data file.

    Returns:
        str: A path such as ``EQT/portfolio/ADAAF/2026/07/20/<filename>``.
    """
    return f"{_medallion_dir(landing.code, landing)}/{landing.filename}"


def _aggregation_dir(code: str) -> str:
    """Return the per-portfolio aggregation directory for a fund code.

    The aggregation layer sits beside the date folders under the shared
    bronze/silver sub-folder, producing ``EQT/portfolio/<code>/aggregation``.

    Args:
        code: The fund family code, for example ``ADAAF``.

    Returns:
        str: The aggregation directory path inside the silver container.
    """
    config = _datalake_config()
    root = str(config["root"])
    subfolder = str(config["bronze_silver_subfolder"])
    aggregation = str(config["aggregation_subfolder"])
    return f"{root}/{subfolder}/{code}/{aggregation}"


def build_aggregation_path(code: str, slug: str, storage_key: str) -> str:
    """Return the aggregation parquet path for one portfolio component.

    Args:
        code: The fund family code, for example ``ADAAF``.
        slug: The portfolio slug embedded in the silver file names.
        storage_key: The report storage key from :func:`report_storage_key`.

    Returns:
        str: A path such as
        ``EQT/portfolio/ADAAF/aggregation/ADAAF_FONDS__attr.parquet``.
    """
    return f"{_aggregation_dir(code)}/{slug}__{storage_key}.parquet"


@dataclass(frozen=True)
class AggregationEntry:
    """A parsed per-portfolio aggregation parquet path.

    Attributes:
        path: The full aggregation path, unchanged.
        code: The fund family code, for example ``ADAAF``.
        slug: The portfolio slug embedded in the file name.
        key: The report storage key, for example ``te`` or ``te_factors``.
    """

    path: str
    code: str
    slug: str
    key: str


def _aggregation_path_pattern() -> re.Pattern[str]:
    """Build the aggregation parquet-path regex from configuration.

    Returns:
        re.Pattern[str]: A compiled pattern with ``code``, ``slug`` and ``key``
        groups. The literal ``aggregation`` folder distinguishes these paths from
        the date-partitioned silver paths.
    """
    config = _datalake_config()
    root = re.escape(str(config["root"]))
    subfolder = re.escape(str(config["bronze_silver_subfolder"]))
    aggregation = re.escape(str(config["aggregation_subfolder"]))
    return re.compile(rf"{root}/{subfolder}/(?P<code>[^/]+)/{aggregation}/(?P<slug>.+)__(?P<key>[^/]+)\.parquet$")


def parse_aggregation_path(path: str) -> AggregationEntry | None:
    """Parse a per-portfolio aggregation parquet path into its components.

    Args:
        path: A full aggregation path such as
            ``EQT/portfolio/ADAAF/aggregation/ADAAF_FONDS__attr.parquet``.

    Returns:
        AggregationEntry | None: The parsed parts, or ``None`` when the path is
        not a recognised aggregation parquet file.
    """
    match = _aggregation_path_pattern().search(path)
    if match is None:
        return None
    return AggregationEntry(
        path=path,
        code=match.group("code").upper(),
        slug=match.group("slug"),
        key=match.group("key"),
    )


def build_silver_path(landing: LandingFile, portfolio: str, storage_key: str) -> str:
    """Return the silver-layer parquet path for one parsed report.

    Args:
        landing: The parsed landing-zone data file the report came from.
        portfolio: The report ``metadata.portfolio`` value.
        storage_key: The report storage key from :func:`report_storage_key`.

    Returns:
        str: A path such as
        ``EQT/portfolio/ADAAF/2026/07/20/EQUS_ALPHA_ADAAF__factor.parquet``.
    """
    directory = _medallion_dir(landing.code, landing)
    return f"{directory}/{portfolio_slug(portfolio)}__{storage_key}.parquet"


@dataclass(frozen=True)
class SilverEntry:
    """A parsed silver-layer parquet path.

    Attributes:
        path: The full silver path, unchanged.
        code: The fund family code, for example ``ADAAF``.
        date: The eight-digit ``YYYYMMDD`` date folder the report belongs to.
        slug: The portfolio slug embedded in the file name.
        key: The report storage key, for example ``te`` or ``te_factors``.
    """

    path: str
    code: str
    date: str
    slug: str
    key: str


def _silver_path_pattern() -> re.Pattern[str]:
    """Build the silver parquet-path regex from configuration.

    Returns:
        re.Pattern[str]: A compiled pattern with ``code``, ``y``, ``m``, ``d``,
        ``slug`` and ``key`` groups.
    """
    config = _datalake_config()
    root = re.escape(str(config["root"]))
    subfolder = re.escape(str(config["bronze_silver_subfolder"]))
    return re.compile(
        rf"{root}/{subfolder}/(?P<code>[^/]+)/(?P<y>\d{{4}})/(?P<m>\d{{2}})/(?P<d>\d{{2}})/"
        rf"(?P<slug>.+)__(?P<key>[^/]+)\.parquet$"
    )


def parse_silver_path(path: str) -> SilverEntry | None:
    """Parse a silver-layer parquet path into its components.

    Args:
        path: A full silver path such as
            ``EQT/portfolio/ADAAF/2026/07/20/ADAAF_FONDS__te.parquet``.

    Returns:
        SilverEntry | None: The parsed parts, or ``None`` when the path is not a
        recognised silver parquet file.
    """
    match = _silver_path_pattern().search(path)
    if match is None:
        return None
    return SilverEntry(
        path=path,
        code=match.group("code").upper(),
        date=f"{match.group('y')}{match.group('m')}{match.group('d')}",
        slug=match.group("slug"),
        key=match.group("key"),
    )

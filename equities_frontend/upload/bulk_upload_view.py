"""Bulk upload page: extract a zip and ingest every supported file inside it."""

import io
import os
import zipfile
from logging import Logger

import pandas as pd
import streamlit as st
from streamlit.logger import get_logger

from helpers import equities_config, get_captured_logs
from upload import upload_model as model
from upload.upload_controller import UploadController

logger: Logger = get_logger(name=__name__)

config = equities_config()
controller = UploadController(config)

st.title("Bulk Upload (zip)")
st.caption(
    "Upload a .zip of research notes and financial models. Every supported file "
    "inside is written to the Data Lake and ingested. Folder structure inside the "
    "zip is ignored — files are flattened by name."
)

uploaded = st.file_uploader("Choose a .zip file", type=["zip"], accept_multiple_files=False)

if uploaded is not None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(uploaded.getvalue()))
    except zipfile.BadZipFile:
        st.error("That doesn't look like a valid zip file.")
        st.stop()

    members = controller.supported_archive_members(archive.namelist())
    skipped = len(archive.namelist()) - len(members)

    if not members:
        st.warning("No supported files found in the archive.")
        st.stop()

    st.write(
        f"Found **{len(members)}** supported file(s)" + (f", skipping {skipped} other entr(y/ies)." if skipped else ".")
    )
    with st.expander("Files to ingest", expanded=False):
        for name in members:
            st.write(f"- {os.path.basename(name)}")

    overwrite = st.checkbox(
        "Overwrite files that are already indexed",
        value=False,
        help="For any file (by filename) that already has chunks in the search index, delete them first. "
        "Otherwise the new content is upserted alongside the old chunks, which can leave stale chunks "
        "behind if a document got shorter.",
    )

    if st.button("Upload and ingest all", type="primary"):
        progress = st.progress(0.0, text="Starting...")
        rows: list[dict] = []
        uploaded_paths: list[str] = []
        total = len(members)

        # 1) upload every file to the lake first
        for i, name in enumerate(members, start=1):
            base = os.path.basename(name)
            try:
                dest_path = controller.upload_document(base, archive.read(name))
                uploaded_paths.append(dest_path)
                rows.append({"file": base, "uploaded": "yes", "chunks": "-", "status": "uploaded"})
            except Exception as error:
                logger.exception(msg=f"Upload failed for {base}: {error}")
                rows.append({"file": base, "uploaded": "no", "chunks": "-", "status": f"upload failed: {error}"})
            progress.progress(model.upload_progress_fraction(i, total), text=f"Uploaded {i}/{total} files")

        # 2) ingest the uploaded files
        results: dict[str, int] = {}
        if uploaded_paths:
            try:

                def _on_progress(path: str, completed: int, total: int) -> None:
                    progress.progress(
                        model.ingest_progress_fraction(completed, total),
                        text=f"Ingesting files... {completed}/{total} ({os.path.basename(path)} done)",
                    )

                results = controller.ingest_documents(uploaded_paths, on_progress=_on_progress, overwrite=overwrite)
            except Exception as error:
                logger.exception(msg=f"Bulk ingestion failed: {error}")
                st.error(f"Files were uploaded, but ingestion failed: {error}")

        # 3) merge ingestion results into the rows
        by_name = {os.path.basename(p): segs for p, segs in results.items()}
        for row in rows:
            segs = by_name.get(row["file"])
            if segs is None:
                continue
            row["chunks"] = f"{segs:,}"
            row["status"] = "ingested" if segs > 0 else "no content"
        progress.progress(1.0, text="Complete")

        ingested = sum(1 for r in rows if r["status"] == "ingested")
        st.success(f"Ingested {ingested}/{total} file(s).")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        with st.expander("Pipeline logs", expanded=False):
            log_lines = get_captured_logs()
            if log_lines:
                st.code("\n".join(log_lines[-100:]), language="log")
            else:
                st.caption("No pipeline logs captured yet.")

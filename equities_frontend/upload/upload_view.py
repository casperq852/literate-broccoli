"""Upload page: add a single document to the Data Lake and ingest it."""

import os
from logging import Logger

import streamlit as st
from streamlit.logger import get_logger

from helpers import equities_config, get_captured_logs
from upload.upload_controller import UploadController

logger: Logger = get_logger(name=__name__)

config = equities_config()
controller = UploadController(config)

st.title("Upload Document")
st.caption(
    "Add a single research note or Bloomberg financial model. The file is written "
    "to the Data Lake and ingested into the search index immediately. Financial "
    "model filenames should start with the configured prefix (e.g. MODL_)."
)

uploaded = st.file_uploader(
    "Choose a file",
    type=[ext.lstrip(".") for ext in controller.allowed_extensions],
    accept_multiple_files=False,
)

if uploaded is not None:
    ext = os.path.splitext(uploaded.name)[-1].lower()
    if ext not in controller.allowed_extensions:
        st.error(f"Unsupported file type {ext}. Allowed: {', '.join(controller.allowed_extensions)}")
        st.stop()

    st.write(f"**{uploaded.name}**  ·  {uploaded.size / 1024:.1f} KB")

    overwrite = st.checkbox(
        "Overwrite if already indexed",
        value=False,
        help="If this document (by filename) already has chunks in the search index, delete them first. "
        "Otherwise the new content is upserted alongside the old chunks, which can leave stale chunks "
        "behind if the document got shorter.",
    )

    if st.button("Upload and ingest", type="primary"):
        try:
            with st.spinner("Uploading to the Data Lake..."):
                dest_path = controller.upload_document(uploaded.name, uploaded.getvalue())
            logger.info(msg=f"Uploaded {dest_path}")
        except Exception as error:
            logger.exception(msg=f"Upload failed for {uploaded.name}: {error}")
            st.error(f"Upload failed: {error}")
            st.stop()

        try:
            with st.spinner("Ingesting into the search index (this can take ~30-60s for models)..."):
                results = controller.ingest_documents([dest_path], overwrite=overwrite)
            segments = results.get(dest_path, 0)
        except Exception as error:
            logger.exception(msg=f"Ingestion failed for {dest_path}: {error}")
            st.error(
                f"The file was uploaded to the Data Lake, but ingestion failed: {error}. " "It can be re-ingested later."
            )
            st.stop()

        if segments > 0:
            st.success(f"Done. Ingested **{uploaded.name}** as {segments:,} searchable chunks.")
        else:
            st.warning(
                f"**{uploaded.name}** was uploaded but produced no searchable content. "
                "Check that the file is a supported, non-empty document."
            )

        with st.expander("Pipeline logs", expanded=False):
            log_lines = get_captured_logs()
            if log_lines:
                st.code("\n".join(log_lines[-100:]), language="log")
            else:
                st.caption("No pipeline logs captured yet.")

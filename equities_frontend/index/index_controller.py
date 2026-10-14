"""Controller for the Index Browser topic: local cache + Azure sync wiring."""

from __future__ import annotations

import pandas as pd

from database.cache import get_all_documents, sync_from_azure
from search import search_model


class IndexController:
    """Wires the local document cache and Azure AI Search sync for the Index Browser."""

    def __init__(self, index_name: str) -> None:
        self._index_name = index_name

    def load_documents(self) -> pd.DataFrame:
        """
        Read document metadata from the local SQLite cache (instant) as a DataFrame.

        Returns:
            pd.DataFrame: One row per document, columns File/Date/Publisher/Type/
            Source/Companies/_path, sorted newest-first.
        """
        docs = get_all_documents()
        rows = [
            {
                "File": doc["filename"],
                "Date": (doc.get("document_date") or "")[:10],
                "Publisher": doc.get("publisher") or "",
                "Type": (doc.get("document_type") or "").replace("_", " "),
                "Source": doc.get("source_type") or "",
                "Companies": ", ".join(doc.get("companies") or []),
                "_path": doc["path"],
            }
            for doc in docs
        ]
        dataframe = (
            pd.DataFrame(rows)
            if rows
            else pd.DataFrame(columns=["File", "Date", "Publisher", "Type", "Source", "Companies", "_path"])
        )
        if not dataframe.empty:
            dataframe = dataframe.sort_values("Date", ascending=False).reset_index(drop=True)
        return dataframe

    def sync_from_azure(self) -> int:
        """
        Re-fetch all document metadata from Azure AI Search into the local cache.

        Returns:
            int: Number of documents synced.
        """
        return sync_from_azure(search_model.get_search_client(self._index_name))

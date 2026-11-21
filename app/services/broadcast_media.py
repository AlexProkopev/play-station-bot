from __future__ import annotations

import base64
import json
import logging
import os
import re
from copy import deepcopy

from app.services.postgres_store import PostgresDocumentStore

logger = logging.getLogger(__name__)

STORAGE_DIR = os.path.join("storage", "broadcast_media")
DOC_PREFIX = "auto_broadcast_media:"
MAX_MEDIA_BYTES = 18 * 1024 * 1024


def media_doc_key(worker_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(worker_id or "default"))[:48]
    return f"{DOC_PREFIX}{safe or 'default'}"


def _local_path(worker_id: str, *, create_dir: bool = False) -> str:
    if create_dir:
        os.makedirs(STORAGE_DIR, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(worker_id or "default"))[:48]
    return os.path.join(STORAGE_DIR, f"{safe or 'default'}.json")


class BroadcastMediaStore:
    @classmethod
    def save(cls, worker_id: str, bundle: dict) -> dict:
        stored = {
            "caption_html": (bundle or {}).get("caption_html") or None,
            "items": [],
        }
        for raw in (bundle or {}).get("items") or []:
            data = raw.get("data")
            b64 = raw.get("data_b64")
            if data is not None and not b64:
                if not isinstance(data, (bytes, bytearray)):
                    continue
                b64 = base64.b64encode(bytes(data)).decode("ascii")
            if not b64:
                continue
            stored["items"].append(
                {
                    "type": str(raw.get("type") or "document"),
                    "filename": str(raw.get("filename") or "file.bin")[:200],
                    "mime": raw.get("mime"),
                    "data_b64": b64,
                }
            )
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(media_doc_key(worker_id), stored, force=True)
            return stored
        path = _local_path(worker_id, create_dir=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(stored, handle, ensure_ascii=False)
        return stored

    @classmethod
    def load(cls, worker_id: str) -> dict | None:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(media_doc_key(worker_id), {})
            if isinstance(data, dict) and data.get("items"):
                return deepcopy(data)
            return None
        path = _local_path(worker_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            logger.exception("Failed to read broadcast media %s", worker_id)
            return None
        if isinstance(data, dict) and data.get("items"):
            return data
        return None

    @classmethod
    def load_decoded(cls, worker_id: str) -> dict | None:
        bundle = cls.load(worker_id)
        if not bundle:
            return None
        items = []
        for raw in bundle.get("items") or []:
            b64 = raw.get("data_b64")
            if not b64:
                continue
            try:
                data = base64.b64decode(b64)
            except Exception:
                continue
            if not data:
                continue
            items.append(
                {
                    "type": str(raw.get("type") or "document"),
                    "filename": str(raw.get("filename") or "file.bin"),
                    "mime": raw.get("mime"),
                    "data": data,
                }
            )
        if not items:
            return None
        return {"caption_html": bundle.get("caption_html"), "items": items}

    @classmethod
    def delete(cls, worker_id: str) -> None:
        if PostgresDocumentStore.is_enabled():
            try:
                PostgresDocumentStore.set_document(media_doc_key(worker_id), {}, force=True)
            except Exception:
                logger.exception("Failed to clear broadcast media %s", worker_id)
        path = _local_path(worker_id)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            logger.exception("Failed to remove local broadcast media %s", worker_id)

"""Authorized image and audio objects linked to an already ingested span."""

import base64
import binascii
import hashlib
import re

from datalog.auth.access import AccessError
from datalog.signals.otlp import checked_id
from datalog.core.validation import Invalid

MAX_MEDIA = 1024 * 1024
MAX_ITEMS = 4
MEDIA = {
    "image/png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR",
    "audio/wav": lambda data: data.startswith(b"RIFF") and data[8:12] == b"WAVE",
}


def span_key(project, params):
    return project, checked_id(params.get("trace_id"), 32), checked_id(params.get("span_id"), 16)


class TraceMedia:
    def __init__(self, connect):
        self.connect = connect

    def add(self, project, body):
        if not isinstance(body, dict):
            raise Invalid("Send a media object")
        ids = span_key(project, body)
        content_type, encoded, digest = (body.get(key) for key in ("content_type", "data", "sha256"))
        if content_type not in MEDIA or not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_MEDIA + 2) // 3):
            raise Invalid("Upload a PNG or WAV up to 1 MiB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise Invalid("Invalid Base64 media") from None
        if len(data) > MAX_MEDIA or not MEDIA[content_type](data) or hashlib.sha256(data).hexdigest() != digest:
            raise Invalid("Invalid media type or SHA-256")
        with self.connect() as db:
            capture = db.execute("SELECT capture_content FROM projects WHERE id=%s FOR SHARE", (project,)).fetchone()
            if not capture or not capture["capture_content"]:
                raise AccessError(409, "Content capture is disabled")
            if not db.execute(
                "SELECT 1 FROM otlp_spans WHERE project=%s AND trace_id=%s AND span_id=%s FOR NO KEY UPDATE", ids
            ).fetchone():
                raise AccessError(404, "Span not found in this project")
            items = db.execute(
                "SELECT digest FROM trace_media WHERE project=%s AND trace_id=%s AND span_id=%s", ids
            ).fetchall()
            if len(items) >= MAX_ITEMS and not any(item["digest"] == digest for item in items):
                raise AccessError(409, "Limit of 4 media items per span")
            db.execute(
                "INSERT INTO trace_media VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (*ids, digest, content_type, data),
            )
        return {"sha256": digest, "content_type": content_type, "bytes": len(data)}

    @staticmethod
    def list(db, ids):
        return db.execute(
            """SELECT m.digest sha256,m.content_type,octet_length(m.content) bytes
            FROM trace_media m JOIN projects p ON p.id=m.project AND p.capture_content
            WHERE m.project=%s AND m.trace_id=%s AND m.span_id=%s ORDER BY m.digest""",
            ids,
        ).fetchall()

    def get(self, project, params):
        digest = params.get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Invalid("Invalid SHA-256")
        with self.connect() as db:
            item = db.execute(
                """SELECT m.content_type,m.content FROM trace_media m
                JOIN projects p ON p.id=m.project AND p.capture_content
                WHERE m.project=%s AND m.trace_id=%s AND m.span_id=%s AND m.digest=%s""",
                (*span_key(project, params), digest),
            ).fetchone()
        if not item:
            raise AccessError(404, "Media not found")
        return item

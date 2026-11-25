"""Transparent at-rest encryption column types (Fernet / AES-128-CBC + HMAC).

Application code reads and writes plain Python values; PostgreSQL only ever
stores ``enc:<fernet token>`` strings for these columns.

* EncryptedText      - TEXT column, whole value encrypted
* EncryptedJSON      - TEXT column holding an encrypted JSON document
* EncryptedMessages  - JSONB list of message dicts; the structure
                       ({"chatno": 1, "prompt": ..., "reply": ...}) is kept but
                       the content fields of every dict are encrypted
"""
from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

from app.security.crypto import decrypt, encrypt, hmac_hex

PREFIX = "enc:"


def _enc(s: str) -> str:
    return PREFIX + encrypt(s)


def _dec(s: str) -> str:
    if isinstance(s, str) and s.startswith(PREFIX):
        return decrypt(s[len(PREFIX):])
    return s


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if isinstance(value, Decimal | float | int):
            value = str(value)
        return "" if value == "" else _enc(str(value))

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        return _dec(value)


class EncryptedJSON(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        return _enc(json.dumps(value, default=str))

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None or value == "":
            return None
        try:
            return json.loads(_dec(value))
        except ValueError:
            return None


class EncryptedMessages(TypeDecorator):
    impl = JSONB
    cache_ok = True
    FIELDS = ("prompt", "reply", "cards", "location", "refs", "attachments")

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        out = []
        for d in value:
            d = dict(d)
            for f in self.FIELDS:
                if f in d and d[f] not in (None, "", []):
                    d[f] = _enc(json.dumps(d[f], default=str))
            out.append(d)
        return out

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return []
        out = []
        for d in value:
            d = dict(d)
            for f in self.FIELDS:
                v = d.get(f)
                if isinstance(v, str) and v.startswith(PREFIX):
                    try:
                        d[f] = json.loads(_dec(v))
                    except ValueError:
                        d[f] = None
            out.append(d)
        return out


def key_hash(*parts: str) -> str:
    """Keyed hash used for uniqueness/lookups of encrypted keys."""
    return hmac_hex("|".join(p.lower().strip() for p in parts), "memory-key")


def to_float(v) -> float:  # noqa: ANN001
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

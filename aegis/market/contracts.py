"""Typed MarketEvent and NormalizedBar contracts with provenance."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


def payload_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


class MarketEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')

    schema_version: int = 1
    event_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1, max_length=16)
    venue: str = Field(min_length=1)
    asset_class: Literal['us_equity', 'etf', 'index', 'crypto_research'] = 'us_equity'
    currency: str = 'USD'
    event_type: Literal['trade', 'quote', 'bar', 'correction', 'cancel'] = 'bar'
    source: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    feed: Literal['iex', 'sip', 'synthetic', 'historical'] = 'iex'
    source_mode: Literal['live', 'delayed', 'historical', 'synthetic'] = 'live'
    provider_ts: datetime
    receive_ts: datetime
    available_at: datetime
    sequence: str | None = None
    revision_id: int = Field(default=1, ge=1)
    price: float | None = Field(default=None, gt=0)
    open: float | None = Field(default=None, gt=0)
    high: float | None = Field(default=None, gt=0)
    low: float | None = Field(default=None, gt=0)
    close: float | None = Field(default=None, gt=0)
    quantity: float | None = Field(default=None, ge=0)
    volume: float | None = Field(default=None, ge=0)
    adjustment_basis: Literal['raw', 'split_adjusted', 'total_return'] = 'raw'
    entitlement_ref: str = 'unverified'
    raw_payload_hash: str = Field(min_length=16)


class NormalizedBar(BaseModel):
    model_config = ConfigDict(extra='forbid')

    symbol: str
    instrument_id: str
    bar_ts: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    feed: Literal['iex', 'sip', 'synthetic', 'historical']
    coverage: Literal['single_exchange', 'consolidated', 'synthetic', 'historical']
    source_mode: Literal['live', 'delayed', 'historical', 'synthetic']
    revision_id: int = Field(default=1, ge=1)
    watermark_ts: datetime
    event_count: int = Field(default=1, ge=1)


import sys
from pathlib import Path

# Make backend/main.py importable when running pytest from backend/ or repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
import respx

import main


# Sample game-API payload matching the live tuple layout:
# payphone = [id, lon, lat, holder_player_id, status]
SAMPLE_PHONES = {
    "payphones": [
        [1, 138.60, -34.92, 0,   "active"],    # uncaptured
        [2, 138.61, -34.93, 7,   "active"],    # mine (GhostScout)
        [3, 138.62, -34.94, 8,   "active"],    # cellmate
        [4, 138.63, -34.95, 9,   "active"],    # hostile
        [5, 138.64, -34.96, 0,   "removed"],   # not active — must be filtered out
        [6, 138.65, -34.97, "0", "active"],    # uncaptured (string zero holder)
    ],
    "players": {
        "7": {"name": "GhostScout", "cellId": 3},
        "8": {"name": "Mate",       "cellId": 3},
        "9": {"name": "<b>Rival</b>", "cellId": 4},
    },
    "cells": {
        "3": {"tag": "WZRD"},
        "4": {"tag": "FOE"},
    },
}


@pytest.fixture(autouse=True)
def _reset_caches():
    main.reset_caches()
    yield
    main.reset_caches()


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Keep failure-path tests fast: no real backoff sleeps."""
    monkeypatch.setattr(main, "_UPSTREAM_RETRY_DELAY", 0.0)


@pytest.fixture
async def client():
    # ASGITransport talks straight to the app; respx only patches the real
    # HTTP transports, so app-internal upstream calls are what gets mocked.
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def mock_api():
    with respx.mock(assert_all_called=False) as router:
        yield router


def mock_phones_ok(router, payload=None):
    """Register a healthy phones-API route; returns the respx route handle."""
    return router.get(main.PHONES_URL).mock(
        return_value=httpx.Response(200, json=payload or SAMPLE_PHONES)
    )

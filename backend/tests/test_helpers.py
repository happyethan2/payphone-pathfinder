"""Unit tests for the pure helper functions."""
import math

import main
from conftest import SAMPLE_PHONES


# ── _classify_phone ─────────────────────────────────────────────────

def _phone(holder):
    return [1, 138.6, -34.9, holder, "active"]


def test_classify_uncaptured_zero_int():
    assert main._classify_phone(_phone(0), "7", set()) == "uncaptured"


def test_classify_uncaptured_zero_str():
    assert main._classify_phone(_phone("0"), "7", set()) == "uncaptured"


def test_classify_uncaptured_none():
    assert main._classify_phone(_phone(None), "7", set()) == "uncaptured"


def test_classify_mine():
    assert main._classify_phone(_phone(7), "7", set()) == "mine"


def test_classify_cellmate():
    assert main._classify_phone(_phone(8), "7", {"8"}) == "cellmate"


def test_classify_hostile():
    assert main._classify_phone(_phone(9), "7", {"8"}) == "hostile"


def test_classify_no_player_resolved():
    # Unknown username: nothing is "mine", holders are hostile
    assert main._classify_phone(_phone(7), None, set()) == "hostile"


# ── _resolve_player ─────────────────────────────────────────────────

def test_resolve_player_by_name():
    pid, mates, players = main._resolve_player(SAMPLE_PHONES, "GhostScout", None)
    assert pid == "7"
    assert mates == {"8"}
    assert players is SAMPLE_PHONES["players"]


def test_resolve_player_case_insensitive():
    pid, mates, _ = main._resolve_player(SAMPLE_PHONES, "ghostscout", None)
    assert pid == "7"
    assert mates == {"8"}


def test_resolve_player_unknown():
    pid, mates, _ = main._resolve_player(SAMPLE_PHONES, "NoSuchPlayer", None)
    assert pid is None
    assert mates == set()


def test_resolve_player_cell_tag_fallback():
    # Player not in the API yet, but cell tag WZRD resolves cellmates
    pid, mates, _ = main._resolve_player(SAMPLE_PHONES, "BrandNewPlayer", "wzrd")
    assert pid is None
    assert mates == {"7", "8"}


# ── _haversine_km ───────────────────────────────────────────────────

def test_haversine_adelaide_melbourne():
    # Adelaide → Melbourne is ~655 km great-circle
    d = main._haversine_km(-34.93, 138.60, -37.81, 144.96)
    assert math.isclose(d, 655, rel_tol=0.03)


def test_haversine_zero():
    assert main._haversine_km(-34.9, 138.6, -34.9, 138.6) == 0.0

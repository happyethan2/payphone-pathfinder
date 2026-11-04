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


# ── _assign_sections ────────────────────────────────────────────────
# Anchors along a line of latitude, 0.1° apart: A(138.0) B(138.1) C(138.2)
A, B, C = (-34.0, 138.0), (-34.0, 138.1), (-34.0, 138.2)


def test_assign_sections_by_least_detour():
    phones = [(-34.0, 138.05), (-34.0, 138.15), (-34.01, 138.04), (-34.02, 138.18)]
    assert main._assign_sections([A, B, C], True, phones) == [0, 1, 0, 1]


def test_assign_sections_tie_goes_earlier():
    # On the stop itself: no detour for either section
    assert main._assign_sections([A, B, C], True, [B]) == [0]


def test_assign_sections_open_last_section():
    # No end: past the last stop is the open section, measured from that stop
    phones = [(-34.0, 138.05), (-34.0, 138.3), (-34.0, 138.12)]
    assert main._assign_sections([A, B], False, phones) == [0, 1, 1]


def test_assign_sections_single_section():
    phones = [(-34.5, 139.0), (-33.0, 137.0)]
    assert main._assign_sections([A, C], True, phones) == [0, 0]
    assert main._assign_sections([A], False, phones) == [0, 0]


# ── _simplify ───────────────────────────────────────────────────────

def test_simplify_collinear_collapse():
    line = [[138.0 + i * 0.001, -34.0] for i in range(50)]
    assert main._simplify(line) == [line[0], line[-1]]


def test_simplify_keeps_a_bump():
    # 0.001° of latitude is about 110 m off the line, far over the 20 m tolerance
    line = [[138.000, -34.0], [138.001, -34.0], [138.002, -34.001], [138.003, -34.0], [138.004, -34.0]]
    out = main._simplify(line)
    assert line[2] in out and out[0] == line[0] and out[-1] == line[-1]


def test_simplify_small_wobble_dropped():
    line = [[138.000, -34.0], [138.001, -34.0001], [138.002, -34.0]]  # ~11 m: under tolerance
    assert main._simplify(line) == [line[0], line[-1]]


def test_simplify_short_lines_unchanged():
    assert main._simplify([]) == []
    assert main._simplify([[1.0, 2.0]]) == [[1.0, 2.0]]
    assert main._simplify([[1.0, 2.0], [1.0, 2.0]]) == [[1.0, 2.0], [1.0, 2.0]]

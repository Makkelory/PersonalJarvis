"""Spoken Russian program names and the program catalog (fork addition)."""
from __future__ import annotations

from jarvis.plugins.tool import app_catalog

_ENTRIES = [
    {"name": "Steam", "key": "steam", "kind": "lnk", "target": "C:/x/Steam.lnk", "exe": "C:/s/steam.exe"},
    {"name": "Spotify", "key": "spotify", "kind": "lnk", "target": "C:/x/Spotify.lnk", "exe": "C:/s/Spotify.exe"},
    {"name": "Telegram", "key": "telegram", "kind": "lnk", "target": "C:/x/Telegram.lnk", "exe": ""},
    {"name": "obs64", "key": "obs64", "kind": "scan", "target": "C:/o/obs64.exe", "exe": "C:/o/obs64.exe"},
]


def test_russian_names_map_to_product_names() -> None:
    assert app_catalog.spoken_to_canonical("Стим") == "steam"
    assert app_catalog.spoken_to_canonical("спотифай") == "spotify"
    assert app_catalog.spoken_to_canonical("неизвестная") == "неизвестная"


def test_lookup_resolves_spoken_names() -> None:
    assert app_catalog.lookup("стим", _ENTRIES)["name"] == "Steam"
    assert app_catalog.lookup("телеграм", _ENTRIES)["name"] == "Telegram"
    assert app_catalog.lookup("абракадабра", _ENTRIES) is None


def test_the_alias_obs_matches_the_versioned_executable() -> None:
    assert app_catalog.lookup("obs", _ENTRIES)["name"] == "obs64"

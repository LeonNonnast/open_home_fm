"""Example plugin: reads a local JSON 'database' of favorite songs per person.

This is the template for "small python scripts as tools" - a plugin can read from any local
data source (a JSON file here, but just as easily a SQLite DB, a CSV, ...) as long as
`execute()` returns a string.
"""
import json
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "favorites.json"


def execute(person: str) -> str:
    if not DATA_PATH.exists():
        return "Keine Favoriten-Datenbank gefunden."

    favorites = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    songs = favorites.get(person)
    if not songs:
        known = ", ".join(favorites.keys()) or "niemand"
        return f"Keine Favoriten für '{person}' bekannt. Bekannt sind: {known}."
    return "\n".join(f"- {song}" for song in songs)

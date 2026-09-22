"""Example plugin: latest headlines from a public RSS feed (no API key required)."""
import urllib.request
import xml.etree.ElementTree as ET

FEED_URL = "https://www.tagesschau.de/xml/rss2/"


def execute(limit: int = 3) -> str:
    try:
        with urllib.request.urlopen(FEED_URL, timeout=10) as response:
            data = response.read()
    except Exception as exc:
        return f"News konnten nicht geladen werden: {exc}"

    root = ET.fromstring(data)
    titles = [t for t in (item.findtext("title") for item in root.findall(".//item")) if t][:limit]

    if not titles:
        return "Keine aktuellen Nachrichten gefunden."
    return "\n".join(f"- {title}" for title in titles)

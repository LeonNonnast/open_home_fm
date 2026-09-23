"""Example plugin: current weather via Open-Meteo (free, no API key required).

The station's home location is chosen once in the installer (install() below) and stored as
coordinates, so the automatic context call doesn't depend on an ambiguous place name.
"""
import httpx

from app.config import load_plugin_settings

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
FALLBACK_LOCATION = "Berlin"


def _geocode(client: httpx.Client, name: str, count: int = 1) -> list[dict]:
    geo = client.get(GEOCODE_URL, params={"name": name, "count": count, "language": "de"}).json()
    return geo.get("results") or []


def _label(place: dict) -> str:
    return ", ".join(part for part in (place.get("name"), place.get("admin1"), place.get("country")) if part)


def execute(location: str | None = None) -> str:
    home = load_plugin_settings("weather")
    with httpx.Client(timeout=10) as client:
        if location and location.strip().lower() != str(home.get("location", "")).lower():
            results = _geocode(client, location)
            if not results:
                return f"Kein Ort namens '{location}' gefunden."
            place = results[0]
        elif "latitude" in home:
            place = {"name": home["location"], "latitude": home["latitude"], "longitude": home["longitude"]}
        else:
            results = _geocode(client, FALLBACK_LOCATION)
            if not results:
                return f"Kein Ort namens '{FALLBACK_LOCATION}' gefunden."
            place = results[0]

        weather = client.get(
            WEATHER_URL,
            params={"latitude": place["latitude"], "longitude": place["longitude"], "current_weather": True},
        ).json()
        current = weather.get("current_weather", {})

    return (
        f"Aktuelles Wetter in {place['name']}: {current.get('temperature')}°C, "
        f"Wind {current.get('windspeed')} km/h."
    )


def install(setup) -> dict:
    """Asks for the station's home location and pins it to one geocoding result."""
    with httpx.Client(timeout=10) as client:
        while True:
            query = setup.ask("Standort für das Wetter (Ortsname)", setup.settings.get("location", FALLBACK_LOCATION))
            results = _geocode(client, query, count=8)
            if not results:
                setup.note(f"Kein Ort namens '{query}' gefunden - bitte anders schreiben.")
                continue
            index = 0
            if len(results) > 1:
                index = setup.ask_choice("Welcher Ort?", [_label(r) for r in results])
            place = results[index]
            setup.note(f"Gespeichert: {_label(place)}")
            return {
                "location": place["name"],
                "label": _label(place),
                "latitude": place["latitude"],
                "longitude": place["longitude"],
            }

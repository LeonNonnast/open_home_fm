"""Example plugin: current weather via Open-Meteo (free, no API key required)."""
import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


def execute(location: str) -> str:
    with httpx.Client(timeout=10) as client:
        geo = client.get(GEOCODE_URL, params={"name": location, "count": 1, "language": "de"}).json()
        results = geo.get("results")
        if not results:
            return f"Kein Ort namens '{location}' gefunden."

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

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


# WMO weather codes (Open-Meteo), coarse German words.
WEATHER_WORDS = {
    0: "klar", 1: "überwiegend klar", 2: "teils bewölkt", 3: "bedeckt", 45: "Nebel", 48: "Nebel",
    51: "Nieselregen", 53: "Nieselregen", 55: "Nieselregen", 61: "leichter Regen", 63: "Regen",
    65: "starker Regen", 66: "gefrierender Regen", 67: "gefrierender Regen", 71: "leichter Schneefall",
    73: "Schneefall", 75: "starker Schneefall", 77: "Schneegriesel", 80: "Regenschauer", 81: "Regenschauer",
    82: "heftige Regenschauer", 85: "Schneeschauer", 86: "Schneeschauer", 95: "Gewitter", 96: "Gewitter mit Hagel",
    99: "Gewitter mit Hagel",
}


def _forecast_lines(daily: dict) -> list[str]:
    lines = []
    for i, label in enumerate(("Heute", "Morgen")):
        try:
            high = daily["temperature_2m_max"][i]
            low = daily["temperature_2m_min"][i]
        except (KeyError, IndexError, TypeError):
            break
        words = WEATHER_WORDS.get((daily.get("weathercode") or [None] * 2)[i], "")
        rain = (daily.get("precipitation_probability_max") or [None] * 2)[i]
        line = f"{label}: {words + ', ' if words else ''}{low} bis {high}°C"
        if rain is not None:
            line += f", Regenwahrscheinlichkeit {rain}%"
        lines.append(line + ".")
    return lines


def execute(location: str | None = None, forecast: bool = False) -> str:
    forecast = forecast is True or str(forecast).strip().lower() in ("true", "1", "ja", "yes")
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

        params = {"latitude": place["latitude"], "longitude": place["longitude"], "current_weather": True}
        if forecast:
            params.update(daily="weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                          timezone="auto", forecast_days=2)
        weather = client.get(WEATHER_URL, params=params).json()
        current = weather.get("current_weather", {})

    now_words = WEATHER_WORDS.get(current.get("weathercode"))
    text = (
        f"Aktuelles Wetter in {place['name']}: {current.get('temperature')}°C{', ' + now_words if now_words else ''}, "
        f"Wind {current.get('windspeed')} km/h."
    )
    if forecast:
        text += "\n" + "\n".join(_forecast_lines(weather.get("daily") or {}))
    return text.strip()


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

"""Philips Hue: switch/dim/color lights via the bridge's local REST API (v1, plain HTTP on the LAN).

Bridge address and default room are chosen in the installer (install() below); the app key the
bridge hands out after pressing its link button is a credential and lives in .env as HUE_APP_KEY.
"""
import os
import re
import socket

import httpx
from rapidfuzz import fuzz, process

from app.config import load_plugin_settings

DISCOVERY_URL = "https://discovery.meethue.com"

# Color temperature in mired (153 = coldest, 500 = warmest) for the white tones.
WHITE_TONES = {"warmweiß": 454, "warmweiss": 454, "neutralweiß": 300, "neutralweiss": 300,
               "weiß": 300, "weiss": 300, "kaltweiß": 200, "kaltweiss": 200}
COLORS = {"rot": "#ff0000", "orange": "#ff7a00", "gelb": "#ffd000", "grün": "#00ff00", "gruen": "#00ff00",
          "türkis": "#00e5d0", "tuerkis": "#00e5d0", "blau": "#0030ff", "lila": "#8a00ff", "violett": "#8a00ff",
          "pink": "#ff2090", "rosa": "#ff80c0"}


def _rgb_to_xy(hex_color: str) -> list[float]:
    """sRGB hex -> CIE xy, per Philips' reference conversion (wide gamut)."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    r, g, b = (((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92 for c in (r, g, b))
    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x + y + z
    return [0.3227, 0.329] if total == 0 else [round(x / total, 4), round(y / total, 4)]


def _color_state(color: str) -> dict:
    key = color.strip().lower()
    if key in WHITE_TONES:
        return {"ct": WHITE_TONES[key]}
    hex_color = COLORS.get(key, key)
    if not re.fullmatch(r"#[0-9a-f]{6}", hex_color):
        raise ValueError(f"Unbekannte Farbe '{color}'")
    return {"xy": _rgb_to_xy(hex_color)}


def _api_base(bridge_ip: str, app_key: str) -> str:
    return f"http://{bridge_ip}/api/{app_key}"


def _check_errors(response: httpx.Response) -> list:
    data = response.json()
    errors = [item["error"]["description"] for item in data if isinstance(item, dict) and "error" in item] \
        if isinstance(data, list) else []
    if errors:
        raise RuntimeError("; ".join(errors))
    return data


def _normalize(name: str) -> str:
    # The LLM may write "Kueche" for "Küche" - fold umlauts so fuzzy matching sees them as equal.
    name = name.lower()
    for umlaut, ascii_form in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        name = name.replace(umlaut, ascii_form)
    return name


def _resolve_target(client: httpx.Client, base: str, target: str) -> tuple[str, str, str]:
    """Finds a room/zone or single light by (fuzzy) name -> (kind, id, display name)."""
    groups = client.get(f"{base}/groups").json()
    lights = client.get(f"{base}/lights").json()
    candidates = {f"groups/{gid}": g["name"] for gid, g in groups.items() if g.get("type") in ("Room", "Zone", "LightGroup")}
    candidates |= {f"lights/{lid}": l["name"] for lid, l in lights.items()}
    match = process.extractOne(target, candidates, scorer=fuzz.WRatio, processor=_normalize, score_cutoff=70)
    if match is None:
        raise ValueError(f"Kein Raum/keine Lampe namens '{target}'. Verfügbar: {', '.join(sorted(set(candidates.values())))}")
    name, _score, path = match
    kind, obj_id = path.split("/")
    return kind, obj_id, name


def _status(client: httpx.Client, base: str) -> str:
    groups = client.get(f"{base}/groups").json()
    lights = client.get(f"{base}/lights").json()
    lines = []
    for group in groups.values():
        if group.get("type") not in ("Room", "Zone"):
            continue
        members = [lights[lid] for lid in group.get("lights", []) if lid in lights]
        on = [l for l in members if l["state"].get("on")]
        detail = ", ".join(
            f"{l['name']} {round(l['state'].get('bri', 254) / 254 * 100)}%" for l in on
        ) or "alles aus"
        lines.append(f"- {group['name']} ({group['type']}): {len(on)}/{len(members)} an - {detail}")
    return "\n".join(lines) or "Keine Räume auf der Bridge eingerichtet."


def execute(action: str, target: str | None = None, brightness: int | None = None, color: str | None = None) -> str:
    settings = load_plugin_settings("hue")
    bridge_ip, app_key = settings.get("bridge_ip"), os.environ.get("HUE_APP_KEY")
    if not bridge_ip or not app_key:
        return "Hue ist nicht eingerichtet - scripts/setup_plugins.py hue ausführen."

    base = _api_base(bridge_ip, app_key)
    with httpx.Client(timeout=5) as client:
        if action == "status":
            return _status(client, base)

        target = target or settings.get("default_target")
        if target:
            kind, obj_id, name = _resolve_target(client, base, target)
        else:
            kind, obj_id, name = "groups", "0", "alle Lampen"

        state: dict = {"on": action == "on"}
        if action == "on":
            if brightness is not None:
                state["bri"] = max(1, min(254, round(int(brightness) / 100 * 254)))
            if color:
                state |= _color_state(color)
        endpoint = f"{base}/{kind}/{obj_id}/" + ("action" if kind == "groups" else "state")
        _check_errors(client.put(endpoint, json=state))

    if action == "off":
        return f"{name}: ausgeschaltet."
    extras = [f"{brightness}%" if brightness is not None else "", color or ""]
    return f"{name}: eingeschaltet" + (f" ({', '.join(e for e in extras if e)})" if any(extras) else "") + "."


def install(setup) -> dict:
    """Finds the bridge, pairs via the link button, and asks for the default room."""
    bridges = []
    try:
        bridges = [b["internalipaddress"] for b in httpx.get(DISCOVERY_URL, timeout=5).json()]
    except Exception:
        setup.note("Automatische Bridge-Suche fehlgeschlagen - IP bitte manuell eingeben.")
    default_ip = setup.settings.get("bridge_ip") or (bridges[0] if bridges else "")
    if bridges:
        setup.note(f"Gefundene Bridge(s): {', '.join(bridges)}")
    bridge_ip = setup.ask("IP-Adresse der Hue Bridge", default_ip)
    if not bridge_ip:
        raise RuntimeError("keine Bridge-IP angegeben")

    with httpx.Client(timeout=5) as client:
        app_key = setup.env("HUE_APP_KEY")
        if app_key and isinstance(client.get(f"{_api_base(bridge_ip, app_key)}/groups").json(), dict):
            setup.note("Bestehende Kopplung mit der Bridge funktioniert.")
        else:
            app_key = ""
            while not app_key:
                input("    Link-Taste auf der Hue Bridge drücken, dann hier Enter (innerhalb 30 s)...")
                response = client.post(
                    f"http://{bridge_ip}/api", json={"devicetype": f"open_home_fm#{socket.gethostname()[:19]}"}
                ).json()
                if response and "success" in response[0]:
                    app_key = response[0]["success"]["username"]
                    setup.note("Gekoppelt.")
                elif not setup.ask_yes_no(f"Kopplung fehlgeschlagen ({response[0]['error']['description']}). Nochmal?", True):
                    raise RuntimeError("Kopplung mit der Bridge abgebrochen")
            setup.set_env("HUE_APP_KEY", app_key)

        groups = client.get(f"{_api_base(bridge_ip, app_key)}/groups").json()
    rooms = sorted(g["name"] for g in groups.values() if g.get("type") in ("Room", "Zone"))
    options = ["alle Lampen"] + rooms
    current = setup.settings.get("default_target")
    choice = setup.ask_choice("Standard-Raum, wenn der Agent keinen nennt", options,
                              options.index(current) if current in options else 0)
    return {"bridge_ip": bridge_ip, "default_target": rooms[choice - 1] if choice else ""}

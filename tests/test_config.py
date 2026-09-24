from __future__ import annotations

import os

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as cfg
from app.api.routes_config import router as config_router
from tests.conftest import DEFAULT_PROMPT, DEFAULTS


def test_deep_merge_dicts_recursive_lists_replaced():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    merged = cfg.deep_merge(base, {"a": {"y": [3]}, "c": None})
    assert merged == {"a": {"x": 1, "y": [3]}, "b": 1, "c": None}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 1}  # inputs untouched


def test_config_diff_is_inverse_of_merge():
    config = cfg.deep_merge(DEFAULTS, {"music": {"provider": "spotify"}, "plugins": {"disabled": []}, "new": {"k": 1}})
    diff = cfg.config_diff(DEFAULTS, config)
    assert diff == {"music": {"provider": "spotify"}, "plugins": {"disabled": []}, "new": {"k": 1}}
    assert cfg.deep_merge(DEFAULTS, diff) == config
    assert cfg.config_diff(DEFAULTS, DEFAULTS) == {}


def test_load_without_user_file_returns_defaults(config_env):
    assert cfg.load_config() == DEFAULTS


def test_save_writes_only_diff_and_roundtrips(config_env):
    config = cfg.load_config()
    config["schedule"]["enabled"] = True
    config["tts"]["piper"]["speaker"] = "neutral"
    cfg.save_config(config)

    stored = yaml.safe_load(cfg.USER_CONFIG_PATH.read_text(encoding="utf-8"))
    assert stored == {"schedule": {"enabled": True}, "tts": {"piper": {"speaker": "neutral"}}}
    assert cfg.load_config() == config


def test_changed_default_reaches_untouched_values(config_env):
    cfg.update_config({"music": {"provider": "spotify"}})
    defaults = yaml.safe_load(cfg.DEFAULTS_PATH.read_text(encoding="utf-8"))
    defaults["llm"]["ollama"]["model"] = "m2"
    cfg.DEFAULTS_PATH.write_text(yaml.safe_dump(defaults), encoding="utf-8")
    loaded = cfg.load_config()
    assert loaded["llm"]["ollama"]["model"] == "m2"
    assert loaded["music"]["provider"] == "spotify"


def test_load_config_cache_sees_hand_edits_and_returns_copies(config_env):
    first = cfg.load_config()
    first["music"]["provider"] = "mutated"
    assert cfg.load_config()["music"]["provider"] == "local"

    cfg.USER_CONFIG_PATH.write_text("music:\n  provider: spotify\n", encoding="utf-8")
    stat = cfg.USER_CONFIG_PATH.stat()
    os.utime(cfg.USER_CONFIG_PATH, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert cfg.load_config()["music"]["provider"] == "spotify"


def test_system_prompt_fallback_save_and_reset(config_env):
    assert cfg.load_system_prompt() == DEFAULT_PROMPT
    assert not cfg.is_prompt_customized()

    cfg.save_system_prompt("Eigener Prompt")
    assert cfg.load_system_prompt() == "Eigener Prompt"
    assert cfg.user_prompt_path().exists()

    # Saving the default text again just follows the default.
    cfg.save_system_prompt(DEFAULT_PROMPT)
    assert not cfg.is_prompt_customized()

    cfg.save_system_prompt("Nochmal eigen")
    assert cfg.reset_system_prompt() == DEFAULT_PROMPT
    assert cfg.load_system_prompt() == DEFAULT_PROMPT


def _client():
    app = FastAPI()
    app.include_router(config_router)
    return TestClient(app), None


def test_patch_merges_partial(config_env):
    client, _ = _client()
    client.patch("/api/config", json={"schedule": {"enabled": True}}).raise_for_status()
    res = client.patch("/api/config", json={"schedule": {"start_time": "07:00"}, "desks": {"music": {"block_minutes": 30}}})
    res.raise_for_status()
    body = res.json()
    assert body["schedule"] == {"enabled": True, "start_time": "07:00", "end_time": "23:00"}
    assert body["desks"]["music"]["block_minutes"] == 30 and body["desks"]["music"]["fill_threshold_minutes"] == 10
    assert yaml.safe_load(cfg.USER_CONFIG_PATH.read_text(encoding="utf-8")) == {
        "schedule": {"enabled": True, "start_time": "07:00"},
        "desks": {"music": {"block_minutes": 30}},
    }


def test_put_replaces_whole_config(config_env):
    client, _ = _client()
    client.patch("/api/config", json={"schedule": {"enabled": True}}).raise_for_status()
    full = client.get("/api/config").json()
    full["schedule"]["enabled"] = False
    full["music"]["provider"] = "spotify"
    client.put("/api/config", json=full).raise_for_status()
    assert client.get("/api/config").json() == full
    assert yaml.safe_load(cfg.USER_CONFIG_PATH.read_text(encoding="utf-8")) == {"music": {"provider": "spotify"}}


def test_system_prompt_api(config_env):
    client, _ = _client()
    assert client.get("/api/config/system_prompt").json() == {"text": DEFAULT_PROMPT, "customized": False}
    assert client.put("/api/config/system_prompt", json={"text": "Neu"}).json()["customized"] is True
    assert client.get("/api/config/system_prompt").json() == {"text": "Neu", "customized": True}
    assert client.post("/api/config/system_prompt/reset").json() == {"text": DEFAULT_PROMPT, "customized": False}
    assert not cfg.user_prompt_path().exists()


def test_stopped_station_is_never_on_air_and_has_no_scheduled_start(config_env):
    from datetime import datetime

    config = cfg.load_config()
    assert not cfg.is_stopped(config) and cfg.is_broadcast_time(config)
    cfg.set_stopped(True)
    config = cfg.load_config()
    assert cfg.is_stopped(config) and cfg.load_user_config() == {"station": {"stopped": True}}
    assert not cfg.is_broadcast_time(config)
    assert cfg.next_broadcast_start(config) is None
    # Also with a schedule: only Play starts the station again, not the next window.
    config["schedule"].update(enabled=True, start_time="06:00", end_time="23:00")
    noon, night = datetime(2026, 1, 1, 12, 0), datetime(2026, 1, 1, 2, 0)
    assert not cfg.is_broadcast_time(config, noon) and cfg.next_broadcast_start(config, night) is None
    cfg.set_stopped(False)
    assert not cfg.is_stopped(cfg.load_config()) and cfg.is_broadcast_time(cfg.load_config())


def test_config_saves_keep_the_stop_state(config_env):
    client, _ = _client()
    stale = client.get("/api/config").json()
    cfg.set_stopped(True)
    assert client.put("/api/config", json=stale).json()["status"] == "ok"
    assert cfg.is_stopped(cfg.load_config())
    assert client.patch("/api/config", json={"station": {"stopped": False}}).json()["station"]["stopped"] is True

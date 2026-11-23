import json

import pytest

from jtec_mcp.config import ConfigError, ServiceConfig, load


def test_load_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[services.slskd]\nurl = "https://s.example/"\nkey_field = "K"\n')
    svc = load(p)["slskd"]
    assert svc.url == "https://s.example" and svc.secret_env == "SLSKD" and svc.key_field == "K"


def test_missing_file_means_no_services(tmp_path):
    assert load(tmp_path / "nope.toml") == {}


def test_api_key_plain_json_and_missing(monkeypatch):
    plain = ServiceConfig("s", "u", "S")
    keyed = ServiceConfig("s", "u", "S", key_field="K")
    monkeypatch.delenv("S", raising=False)
    assert plain.api_key() is None
    monkeypatch.setenv("S", "abc")
    assert plain.api_key() == "abc"
    with pytest.raises(ConfigError):
        keyed.api_key()
    monkeypatch.setenv("S", json.dumps({"K": "xyz", "OTHER": 1}))
    assert keyed.api_key() == "xyz"
    monkeypatch.setenv("S", json.dumps({"OTHER": 1}))
    with pytest.raises(ConfigError):
        keyed.api_key()

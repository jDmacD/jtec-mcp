"""User configuration: which services exist, where they are, and where their API keys live.

The config file holds no secrets. Each service names an environment variable
(filled by secretspec, or anything else) and, when that variable holds a JSON
object, the field inside it that is the API key.

    [services.slskd]
    url = "https://slskd.example.lan"
    secret_env = "SLSKD"          # default: the service name, upper-cased
    key_field = "SLSKD_API_KEY"   # omit if the variable is the bare key

Any other keys in a service's table are service-specific options
(`ServiceConfig.options`), e.g. FileBrowser's upload folder.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "jtec-mcp" / "config.toml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    url: str
    secret_env: str
    key_field: str | None = None
    verify_tls: bool | str = True
    options: dict = field(default_factory=dict, compare=False, hash=False)

    def api_key(self) -> str | None:
        """Return the API key, or None if the secret isn't set."""
        raw = os.environ.get(self.secret_env, "").strip()
        if not raw:
            return None
        if self.key_field is None:
            return raw
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConfigError(
                f"{self.secret_env} is not JSON, but key_field={self.key_field!r} is set"
            ) from e
        value = data.get(self.key_field) if isinstance(data, dict) else None
        if not value:
            raise ConfigError(f"{self.secret_env} has no field {self.key_field!r}")
        return str(value)


_KNOWN = {"url", "secret_env", "key_field", "verify_tls"}


def load(path: str | Path | None = None) -> dict[str, ServiceConfig]:
    path = Path(path or os.environ.get("JTEC_MCP_CONFIG") or default_path())
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text())
    services: dict[str, ServiceConfig] = {}
    for name, s in data.get("services", {}).items():
        if "url" not in s:
            raise ConfigError(f"services.{name} has no url")
        services[name] = ServiceConfig(
            name=name,
            url=s["url"].rstrip("/"),
            secret_env=s.get("secret_env", name.upper()),
            key_field=s.get("key_field"),
            verify_tls=s.get("verify_tls", True),
            options={k: v for k, v in s.items() if k not in _KNOWN},
        )
    return services

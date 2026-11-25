"""Entry point: register tools for each configured service whose secret is present."""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from jtec_mcp import config

log = logging.getLogger("jtec_mcp")

INSTRUCTIONS = """\
Workflow-level tools for self-hosted homelab services. Tools are prefixed by
service (slskd_, arr_, jellyfin_, opnsense_, ...); only services configured on this host
are present.
Results are summarised; ask for detail with the tools' optional arguments.
"""


def _services() -> dict:
    """Service name -> (client class, needs an API key)."""
    from jtec_mcp.arr.client import APPS, ArrClient
    from jtec_mcp.aurral.client import AurralClient
    from jtec_mcp.bazarr.client import BazarrClient
    from jtec_mcp.jellyfin.client import JellyfinClient
    from jtec_mcp.metadata.client import MetadataClient
    from jtec_mcp.opnsense.client import OpnsenseClient
    from jtec_mcp.qbittorrent.client import QbittorrentClient
    from jtec_mcp.slskd.client import SlskdClient

    return {
        "slskd": (SlskdClient, True),
        "aurral": (AurralClient, True),
        "metadata": (MetadataClient, False),
        "jellyfin": (JellyfinClient, True),
        "bazarr": (BazarrClient, True),
        "qbittorrent": (QbittorrentClient, True),
        "opnsense": (OpnsenseClient, True),
        **{app: (ArrClient, True) for app in APPS},
    }


def _register(server: MCPServer, clients: dict) -> None:
    """Add each service's tools. Most take their own client; the Servarr tools
    take every configured app at once, plus qBittorrent to join queues with."""
    from jtec_mcp.arr.client import APPS
    from jtec_mcp.arr.tools import register as register_arr
    from jtec_mcp.aurral.tools import register as register_aurral
    from jtec_mcp.bazarr.tools import register as register_bazarr
    from jtec_mcp.jellyfin.tools import register as register_jellyfin
    from jtec_mcp.metadata.tools import register as register_metadata
    from jtec_mcp.opnsense.tools import register as register_opnsense
    from jtec_mcp.qbittorrent.tools import register as register_qbittorrent
    from jtec_mcp.slskd.tools import register as register_slskd

    single = {
        "slskd": register_slskd,
        "aurral": register_aurral,
        "metadata": register_metadata,
        "jellyfin": register_jellyfin,
        "bazarr": register_bazarr,
        "qbittorrent": register_qbittorrent,
        "opnsense": register_opnsense,
    }
    for name, register in single.items():
        if name in clients:
            register(server, clients[name])
    if arr := {n: clients[n] for n in APPS if n in clients}:
        register_arr(server, arr, clients.get("qbittorrent"))


def build(services: dict[str, config.ServiceConfig] | None = None) -> MCPServer:
    services = config.load() if services is None else services
    server = MCPServer("jtec-mcp", instructions=INSTRUCTIONS)

    clients = {}
    for name, (client_cls, needs_key) in _services().items():
        svc = services.get(name)
        if svc is None:
            continue
        key = svc.api_key()
        if needs_key and not key:
            log.warning("%s configured but %s is not set; skipping", name, svc.secret_env)
            continue
        clients[name] = client_cls(svc, key)
        log.info("%s configured (%s)", name, svc.url)

    _register(server, clients)
    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s: %(message)s")
    # httpx logs every request at INFO; search polling makes that noisy.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build().run()

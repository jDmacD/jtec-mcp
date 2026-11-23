"""Entry point: register tools for each configured service whose secret is present."""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from jtec_mcp import config

log = logging.getLogger("jtec_mcp")

INSTRUCTIONS = """\
Workflow-level tools for self-hosted homelab services. Tools are prefixed by
service (slskd_, ...); only services configured on this host are present.
Results are summarised; ask for detail with the tools' optional arguments.
"""


def _services() -> dict:
    """Service name -> (client class, register function)."""
    from jtec_mcp.aurral.client import AurralClient
    from jtec_mcp.aurral.tools import register as register_aurral
    from jtec_mcp.slskd.client import SlskdClient
    from jtec_mcp.slskd.tools import register as register_slskd

    return {
        "slskd": (SlskdClient, register_slskd),
        "aurral": (AurralClient, register_aurral),
    }


def build(services: dict[str, config.ServiceConfig] | None = None) -> MCPServer:
    services = config.load() if services is None else services
    server = MCPServer("jtec-mcp", instructions=INSTRUCTIONS)

    for name, (client_cls, register) in _services().items():
        svc = services.get(name)
        if svc is None:
            continue
        key = svc.api_key()
        if not key:
            log.warning("%s configured but %s is not set; skipping", name, svc.secret_env)
            continue
        register(server, client_cls(svc, key))
        log.info("%s tools registered (%s)", name, svc.url)

    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s: %(message)s")
    # httpx logs every request at INFO; search polling makes that noisy.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build().run()

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


def build(services: dict[str, config.ServiceConfig] | None = None) -> MCPServer:
    services = config.load() if services is None else services
    server = MCPServer("jtec-mcp", instructions=INSTRUCTIONS)

    if (svc := services.get("slskd")) and (key := svc.api_key()):
        from jtec_mcp.slskd.client import SlskdClient
        from jtec_mcp.slskd.tools import register

        register(server, SlskdClient(svc, key))
        log.info("slskd tools registered (%s)", svc.url)
    elif svc:
        log.warning("slskd configured but %s is not set; skipping", svc.secret_env)

    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s: %(message)s")
    # httpx logs every request at INFO; search polling makes that noisy.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build().run()

"""
sdcmodeler: the modeler's client for SDCStudio.

Three layers in one package (SDCStudio's agent-path PRD, decision D1):

- ``sdcmodeler.client``: a framework-free client for the SDCStudio API, httpx
  and nothing else. Search the catalog, estimate, assemble and publish a data
  model, generate the package and an application, read the wallet and the
  key's policy.
- ``sdcmodeler.cli``: the same client at a terminal.
- ``sdcmodeler.mcp_server``: ``sdcmodeler mcp``, a JSON-RPC 2.0 stdio server
  over the client for any MCP-capable agent, carrying the product policy: no
  spend without an estimate shown, stop at the key's limit, never pay.

The name follows the role-name pattern of the other Semantic Data Charter
servers: sdcvalidator validates, sdcgovernance governs, sdcreader reads,
sdcmodeler models. It also names the concept SDCStudio already has: every user
is a Modeler, and the Modeler's projects scope every call.
"""
__version__ = "4.2.1"

from sdcmodeler.client import (  # noqa: E402
    AssemblyRefused,
    InsufficientCredits,
    KeyPolicyRefused,
    SDCStudioClient,
    SDCStudioError,
)
from sdcmodeler.pricing import estimate_tree  # noqa: E402

__all__ = [
    "__version__",
    "SDCStudioClient",
    "SDCStudioError",
    "InsufficientCredits",
    "KeyPolicyRefused",
    "AssemblyRefused",
    "estimate_tree",
]

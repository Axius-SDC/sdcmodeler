"""
``sdcmodeler mcp``: an MCP stdio server over the SDCStudio client.

JSON-RPC 2.0 over stdio, one object per line, no MCP SDK, the way sdcvalidator,
sdcgovernance and sdcreceipt are built. The server carries the product policy
the client deliberately does not (SDCStudio's agent-path PRD, 3.2):

- **No spend without an estimate shown.** ``build_model`` takes the
  ``estimate_id`` that ``estimate_model`` returned for the same title and tree
  in this process; without it, or with a stale one, it refuses. The agent has
  to put the number in front of the person before anything is built.
- **Stop at the key's limit.** When the key carries a spending limit, a build
  whose estimate exceeds what the key may still spend is refused here, before
  the server is asked.
- **Never pay.** A 402 is reported with the wallet page's address and the tool
  stops. No tool takes a URL or a payment method.

Configuration is the environment the server starts with: ``SDCSTUDIO_API_KEY``
(the key from Settings, API Keys) and ``SDCSTUDIO_URL`` (the deployment; the
production service by default). The key never travels through a tool argument.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from sdcmodeler import __version__
from sdcmodeler.client import (
    AssemblyRefused,
    InsufficientCredits,
    KeyPolicyRefused,
    SDCStudioClient,
    SDCStudioError,
)
from sdcmodeler.pricing import DOWNLOAD_APP_CREDITS

JSONRPC_VERSION = "2.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-11-25")
MCP_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]
SERVER_NAME = "sdcmodeler"
SERVER_VERSION = __version__

_client: SDCStudioClient | None = None
#: estimate_id -> the estimate, for this process: a build must follow an estimate the person saw.
_estimates: dict[str, dict[str, Any]] = {}
_output_dir: Path = Path(os.environ.get("SDCMODELER_OUTPUT_DIR", ".")).resolve()


def configure(client: SDCStudioClient | None = None, output_dir: str | Path | None = None) -> SDCStudioClient:
    """Bind the client (and where downloads land) for this process; tests pass their own."""
    global _client, _output_dir
    _client = client or SDCStudioClient()
    if output_dir is not None:
        _output_dir = Path(output_dir).resolve()
    _estimates.clear()
    return _client


def get_client() -> SDCStudioClient:
    return _client if _client is not None else configure()


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_components",
        "description": (
            "Search SDCStudio's public catalog of published components by label, with optional "
            "type (xdstring, xdcount, xdquantity, xdtemporal, cluster, party, ...) and project. "
            "Reuse first: a published component that fits is free to assemble; minting a new one "
            "costs 100 credits. Returns ct_id, label, description, component_type, project_name "
            "and reuse_ref for each result. Free; no key needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words from the label or description."},
                "type": {"type": "string", "description": "Optional component type to filter by."},
                "project": {"type": "string", "description": "Optional project name to filter by, e.g. a public library."},
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 25},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_models",
        "description": "Published data models in the public catalog, by words in the title. Free.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "default": ""}, "page": {"type": "integer", "default": 1},
                           "page_size": {"type": "integer", "default": 25}},
        },
    },
    {
        "name": "get_model",
        "description": (
            "One published data model: title, description, project, its components, and the artifact "
            "routes (xsd, xml, json, jsonld, html, ttl, shacl, gql, zip). The XSD is the contract every "
            "record validates against. Free."
        ),
        "inputSchema": {"type": "object", "properties": {"ct_id": {"type": "string"}}, "required": ["ct_id"]},
    },
    {
        "name": "estimate_model",
        "description": (
            "What assembling this model would reuse, mint and cost, in credits, computed before any "
            "money moves. Takes the title and the assembly tree ({label, components: [{ct_id} to reuse "
            "or {label, data_type} to mint], clusters: [...]}). Returns estimate_id, the reused and "
            "minted leaves, problems in the tree, and total_credits (500 for the model plus 100 per "
            "mint). Show the result to the person; build_model needs the estimate_id. Free."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "tree": {"type": "object", "description": "The assembly tree."},
            },
            "required": ["title", "tree"],
        },
    },
    {
        "name": "build_model",
        "description": (
            "Assemble and publish the model the person estimated and approved. Needs the estimate_id "
            "from estimate_model for the same title and tree; refuses otherwise, and refuses when the "
            "estimate exceeds what the key may still spend. Spends credits: 500 for the model plus "
            "100 per minted component. With every leaf existing the model is published and its "
            "package generated at once (result has ct_id and artifacts); with new leaves the "
            "pipeline runs and the result has a data_source_ct_id to poll with job_status. A 402 "
            "means the wallet needs funding at the wallet page; report it and stop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "estimate_id": {"type": "string"},
                "title": {"type": "string"},
                "tree": {"type": "object"},
                "description": {"type": "string", "default": ""},
                "contextual": {"type": "object", "description": "Optional contextual component references (audit, attestation, party, subject, provider, participation, protocol, workflow, acs)."},
            },
            "required": ["estimate_id", "title", "tree"],
        },
    },
    {
        "name": "job_status",
        "description": "The pipeline status of a mixed build (data_source_ct_id from build_model): uploaded, parsing, parsed, agent_processing, modeling, completed, error, agent_error. Free.",
        "inputSchema": {"type": "object", "properties": {"data_source_ct_id": {"type": "string"}}, "required": ["data_source_ct_id"]},
    },
    {
        "name": "generate_app",
        "description": (
            "Generate a published model's application (lightweight or enterprise profile; a Django "
            "stack on the model package, one example of implementing on it) and download it into the "
            "server's output directory. The first download of a model by this user costs 1,500 "
            "credits; later downloads of the same model are free. Needs confirm_charge true, after the "
            "person has agreed. Prefer the model package (get_model's artifacts) when the feature "
            "lives inside an existing application."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ct_id": {"type": "string"},
                "profile": {"type": "string", "enum": ["lightweight", "enterprise"], "default": "lightweight"},
                "confirm_charge": {"type": "boolean", "default": False},
            },
            "required": ["ct_id"],
        },
    },
    {
        "name": "wallet",
        "description": "The wallet balance in credits and this key's policy: scopes, spending limit, what it has spent, expiry. Free.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _string(args: dict, name: str, *, required: bool = False, default: str = "") -> str:
    value = args.get(name, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"`{name}` must be a string.")
    if required and not value.strip():
        raise ValueError(f"`{name}` is required.")
    return value


def _object(args: dict, name: str, *, required: bool = False) -> dict | None:
    value = args.get(name)
    if value is None:
        if required:
            raise ValueError(f"`{name}` is required.")
        return None
    if not isinstance(value, dict):
        raise ValueError(f"`{name}` must be an object.")
    return value


def _handle_search_components(args: dict) -> Any:
    c = get_client()
    return c.search_components(_string(args, "query", required=True), type=_string(args, "type") or None,
                               project=_string(args, "project") or None, page=int(args.get("page") or 1),
                               page_size=int(args.get("page_size") or 25))


def _handle_list_models(args: dict) -> Any:
    return get_client().list_models(_string(args, "query"), page=int(args.get("page") or 1), page_size=int(args.get("page_size") or 25))


def _handle_get_model(args: dict) -> Any:
    return get_client().get_model(_string(args, "ct_id", required=True))


def _handle_estimate_model(args: dict) -> Any:
    tree = _object(args, "tree", required=True)
    estimate = get_client().estimate(tree, _string(args, "title", required=True))
    _estimates[estimate["estimate_id"]] = estimate
    policy = _policy_or_none()
    if policy and policy.get("spend_limit_credits") is not None:
        remaining = max(0, policy["spend_limit_credits"] - policy.get("spent_credits", 0))
        estimate["key_remaining_credits"] = remaining
        estimate["within_key_limit"] = estimate["total_credits"] <= remaining
    return estimate


def _policy_or_none() -> dict | None:
    try:
        return get_client().key_policy()
    except SDCStudioError:
        return None


def _handle_build_model(args: dict) -> Any:
    estimate_id = _string(args, "estimate_id", required=True)
    title = _string(args, "title", required=True)
    tree = _object(args, "tree", required=True)
    current = get_client().estimate(tree, title)
    shown = _estimates.get(estimate_id)
    if shown is None or current["estimate_id"] != estimate_id:
        raise ValueError(
            "build_model needs the estimate_id that estimate_model returned for this exact title and tree "
            "in this session; run estimate_model, show the person the cost, then build."
        )
    if current["problems"]:
        raise ValueError("The tree has problems: " + "; ".join(current["problems"]))
    policy = _policy_or_none()
    if policy and policy.get("spend_limit_credits") is not None:
        remaining = max(0, policy["spend_limit_credits"] - policy.get("spent_credits", 0))
        if current["total_credits"] > remaining:
            raise ValueError(
                f"This key may still spend {remaining} credits and the build needs {current['total_credits']}. "
                "Ask the person for a key with a higher limit, or to run the build themselves."
            )
    result = get_client().assemble(title, tree, description=_string(args, "description"), contextual=_object(args, "contextual"))
    result["estimate"] = current
    return result


def _handle_job_status(args: dict) -> Any:
    return get_client().job_status(_string(args, "data_source_ct_id", required=True))


def _handle_generate_app(args: dict) -> Any:
    ct_id = _string(args, "ct_id", required=True)
    profile = _string(args, "profile", default="lightweight") or "lightweight"
    if not args.get("confirm_charge"):
        raise ValueError(
            f"generate_app downloads the application, which costs {DOWNLOAD_APP_CREDITS} credits the first time "
            "this user downloads this model (free after that). Tell the person, and call again with confirm_charge true."
        )
    c = get_client()
    generated = c.generate_app(ct_id, profile)
    path, charged = c.download_app(ct_id, _output_dir, profile)
    return {"generated": generated, "path": str(path), "charged": charged, "profile": profile}


def _handle_wallet(args: dict) -> Any:
    c = get_client()
    return {"wallet": c.wallet(), "key": c.key_policy()}


TOOL_HANDLERS = {
    "search_components": _handle_search_components,
    "list_models": _handle_list_models,
    "get_model": _handle_get_model,
    "estimate_model": _handle_estimate_model,
    "build_model": _handle_build_model,
    "job_status": _handle_job_status,
    "generate_app": _handle_generate_app,
    "wallet": _handle_wallet,
}


def _jsonrpc_response(id: Any, result: Any) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "result": result}


def _jsonrpc_error(id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "error": err}


def _tool_error(req_id: Any, text: str) -> dict:
    # SEP-1303: execution and input-validation failures are tool errors, not
    # protocol errors, so the calling model sees them and can correct itself.
    return _jsonrpc_response(req_id, {"content": [{"type": "text", "text": text}], "isError": True})


def _handle_request(request: Any) -> dict | None:
    """One JSON-RPC 2.0 request; a response, or None for a notification. Malformed input is answered, never fatal."""
    if not isinstance(request, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request: a JSON-RPC request is an object")
    req_id = request.get("id")
    if not isinstance(req_id, (str, int, type(None))):
        return _jsonrpc_error(None, -32600, "Invalid Request: id must be a string, a number or null")
    method = request.get("method", "")
    if not isinstance(method, str):
        return _jsonrpc_error(req_id, -32600, "Invalid Request: method must be a string")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return _jsonrpc_error(req_id, -32600, "Invalid Request: params must be an object")

    if method == "initialize":
        requested = params.get("protocolVersion")
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
        has_key = bool(get_client().api_key)
        return _jsonrpc_response(req_id, {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "The modeler's client for SDCStudio. Search the public catalog first and reuse published "
                "components; estimate before you build and show the person the cost; build only with the "
                "estimate_id you showed; prefer the model package over the generated application when the "
                "feature lives inside an existing application. A 402 means the person funds the wallet at "
                f"{get_client().base_url}/app/wallet; never try to pay. "
                + ("A key is configured, so building and downloading are available within its policy."
                   if has_key else "No key is configured: the catalog and estimates work; building needs SDCSTUDIO_API_KEY.")
            ),
        })
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return _jsonrpc_response(req_id, {"tools": TOOLS})
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        if not isinstance(tool_name, str) or not isinstance(tool_args, dict):
            return _jsonrpc_error(req_id, -32602, "Invalid params: name must be a string and arguments an object")
        handler = TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return _jsonrpc_error(req_id, -32601, f"Unknown tool: {tool_name}")
        try:
            result = handler(tool_args)
            return _jsonrpc_response(req_id, {"content": [{"type": "text", "text": json.dumps(result, default=str)}]})
        except InsufficientCredits as exc:
            return _tool_error(req_id, (
                f"Insufficient credits: this needs {exc.estimated_cost_credits} credits and the wallet has "
                f"{exc.balance_credits}. The person funds the wallet at {get_client().base_url}/app/wallet; stop here."
            ))
        except KeyPolicyRefused as exc:
            return _tool_error(req_id, f"The API key refused this ({exc.error_type}): {exc}")
        except (AssemblyRefused, SDCStudioError, ValueError) as exc:
            return _tool_error(req_id, f"Tool execution error: {exc}")
        except Exception as exc:  # a defect in this server, not in the caller's input
            return _tool_error(req_id, (
                f"Tool execution error: the server could not process this input ({type(exc).__name__}). "
                "This is a defect worth reporting at https://github.com/Axius-SDC/sdcmodeler/issues"
            ))
    if method == "ping":
        return _jsonrpc_response(req_id, {})
    if req_id is not None:
        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")
    return None


def run_stdio() -> None:
    """Read JSON-RPC 2.0 messages from stdin, one per line; write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stdout.write(json.dumps(_jsonrpc_error(None, -32700, f"Parse error: {exc}")) + "\n")
            sys.stdout.flush()
            continue
        response = _handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def main(argv: list[str] | None = None) -> None:
    """Entry point of ``sdcmodeler mcp``: start the stdio server with the environment's key and URL."""
    import argparse

    parser = argparse.ArgumentParser(prog="sdcmodeler mcp", description="MCP stdio server over the SDCStudio client.")
    parser.add_argument("--url", default=None, help="SDCStudio base URL (default: $SDCSTUDIO_URL or the production service).")
    parser.add_argument("--output-dir", default=None, help="Where downloads land (default: $SDCMODELER_OUTPUT_DIR or the current directory).")
    args = parser.parse_args(argv)
    configure(SDCStudioClient(base_url=args.url), output_dir=args.output_dir)
    if sys.stdin.isatty():
        sys.stderr.write("sdcmodeler mcp: reading JSON-RPC 2.0 on stdin (Ctrl-D to stop)\n")
        sys.stderr.flush()
    run_stdio()


if __name__ == "__main__":
    main()

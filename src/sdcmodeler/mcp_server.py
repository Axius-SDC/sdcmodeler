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
- **One approval per run.** ``build_and_deliver`` takes the ``estimate_id``
  that ``estimate_model`` returned with the same ``deliver``, whose total covers
  the build and the deliverables, and refuses before the build when the wallet
  or the key cannot pay that total (``sdcmodeler.deliver``). A key the person
  created with auto-spend in SDCStudio (read from the server, never from an
  argument) runs it without a prior estimate, within the person's settings.
- **The same for a translation.** ``estimate_translation`` never spends, even
  with auto-spend; ``translate_model`` takes the ``estimate_id`` it returned
  for the same source, language, project and review, or runs on an auto-spend key.

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
    TranslationRefused,
)
from sdcmodeler import deliver as runs
from sdcmodeler import translate as translations
from sdcmodeler.kits import KitConflictError, write_kit
from sdcmodeler.pricing import DOWNLOAD_APP_CREDITS

JSONRPC_VERSION = "2.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-11-25")
MCP_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]
SERVER_NAME = "sdcmodeler"
SERVER_VERSION = __version__

_client: SDCStudioClient | None = None
#: estimate_id -> the estimate, for this process: a build must follow an estimate the person saw.
_estimates: dict[str, dict[str, Any]] = {}
#: (estimate_id, deliverables) -> the run's plan, for this process: build_and_deliver follows the total the person saw.
_plans: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
#: estimate_id -> the translation's plan, for this process: translate_model follows the total the person saw.
_translations: dict[str, dict[str, Any]] = {}
_output_dir: Path = Path(os.environ.get("SDCMODELER_OUTPUT_DIR", ".")).resolve()


def configure(client: SDCStudioClient | None = None, output_dir: str | Path | None = None) -> SDCStudioClient:
    """Bind the client (and where downloads land) for this process; tests pass their own."""
    global _client, _output_dir
    _client = client or SDCStudioClient()
    if output_dir is not None:
        _output_dir = Path(output_dir).resolve()
    _estimates.clear()
    _plans.clear()
    _translations.clear()
    return _client


def get_client() -> SDCStudioClient:
    return _client if _client is not None else configure()


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_components",
        "description": (
            "Search published components by label, with optional type (xdstring, xdcount, xdquantity, "
            "xdtemporal, cluster, party, ...) and project. With a key, the Modeler's own published "
            "components come first, private ones included (each result says scope: mine), then the "
            "public catalog (scope: public); without a key, the public catalog only. Reuse your own, "
            "then the public catalog, and mint last: a published component that fits is free to "
            "assemble; a new one costs 200 credits (100 for the draft, 100 to publish it). Returns ct_id, label, description, "
            "component_type, project_name and reuse_ref for each result. Free."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words from the label or description."},
                "type": {"type": "string", "description": "Optional component type to filter by."},
                "project": {"type": "string", "description": "Optional project name to filter by, e.g. a public library."},
                "scope": {"type": "string", "enum": ["mine", "public"],
                          "description": "Optional: mine (your own first, then public; needs a key) or public. Default: mine with a key."},
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
            "minted leaves, problems in the tree, and total_credits (500 for the model, plus 200 for each "
            "new component or cluster: 100 for its draft, 100 to publish it). Show the result to the person; build_model needs the estimate_id. With deliver "
            "(package, app:lightweight, app:enterprise, kit), it prices the whole run for "
            "build_and_deliver: the build plus 1,500 credits once for an application or kit, and under "
            "run the total, what the run will publish (permanently), how it is funded (the balance, or a "
            "reload the person allows), whether the key runs with auto-spend, and refusals. Pass that "
            "estimate_id to build_and_deliver; build_model takes an estimate made without deliver. Free."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "tree": {"type": "object", "description": "The assembly tree."},
                "publish": {"type": "boolean", "default": True,
                            "description": "False prices a draft build (build_model with publish false): no publication charges. Same estimate_id."},
                "deliver": {
                    "type": "array", "items": {"type": "string", "enum": list(runs.DELIVERABLES) + ["app"]},
                    "description": "Optional: what build_and_deliver will deliver; prices the whole run.",
                },
            },
            "required": ["title", "tree"],
        },
    },
    {
        "name": "build_model",
        "description": (
            "Assemble and publish the model the person estimated and approved. Needs the estimate_id "
            "from estimate_model for the same title and tree; refuses otherwise, the server refuses a stale "
            "one too, and refuses when the estimate exceeds what the key may still spend. Spends credits: "
            "500 for the model plus 200 per new component or cluster (100 with publish false). With every leaf existing the "
            "model is published and its package generated at once (result has ct_id and artifacts); with "
            "new leaves the pipeline runs and the result has a job_ct_id to poll with job_status; when the "
            "job completes it has published and generated as asked and carries the model's links. publish "
            "false leaves a draft for a person to review, with bench_url, the page that opens it on the Bench in "
            "SDCStudio (on the job's result for the pipeline path); give the person that link. A 402 "
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
                "publish": {"type": "boolean", "default": True, "description": "Publish the clusters and the model (false leaves a draft for a person to review)."},
                "generate": {"type": "boolean", "default": True, "description": "Build the package after publishing (needs publish)."},
            },
            "required": ["estimate_id", "title", "tree"],
        },
    },
    {
        "name": "build_and_deliver",
        "description": (
            "One run: assemble the model, publish it and every new component into the Modeler's private default "
            "project, wait for the build, then deliver each item of deliver: package (into the output directory), "
            "app:lightweight or app:enterprise (the generated application), kit (the Supabase kit, written into "
            "project_dir). Needs the estimate_id that estimate_model returned for the same title, tree and "
            "deliver, after the person has seen the run's total; that one approval covers the 1,500-credit "
            "application or kit charge, so no confirm_charge. When the person created the key with auto-spend "
            "in SDCStudio, no estimate_id is needed: the tool estimates and builds in one call and returns the "
            "estimate in the result. Refuses before anything is built when the run cannot be paid; a 402 names "
            "the reason (auto-reload off, unconfirmed, at its ceiling, card declined, an organization wallet). "
            "deliver defaults to $SDCMODELER_DELIVER, else package. Once the model is published, a deliverable "
            "that fails is reported under failed and the others still run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "estimate_id": {"type": "string", "description": "From estimate_model with deliver; not needed with an auto-spend key."},
                "title": {"type": "string"},
                "tree": {"type": "object"},
                "deliver": {"type": "array", "items": {"type": "string", "enum": list(runs.DELIVERABLES) + ["app"]}},
                "project_dir": {
                    "type": "string",
                    "description": "For kit: the project holding supabase/, relative to the output directory. Default: $SDCMODELER_PROJECT_DIR, else the output directory.",
                },
                "description": {"type": "string", "default": ""},
            },
            "required": ["title", "tree"],
        },
    },
    {
        "name": "estimate_translation",
        "description": (
            "What translating a published model or component into another language would cost, before any money "
            "moves; never spends, even with an auto-spend key. SDCStudio copies every component the source needs "
            "into the target language, each keeping its link to its original, its semantic links and its "
            "enumeration definition IRIs, and translates the human text (labels, descriptions, enumeration values, "
            "boolean values, ordinal symbols, the model's title and description, across its data tree); the "
            "contextual components (audit, attestation, access control, protocol, workflow, parties) and Units stay "
            "in their original language, and links and constraints never change. The language is any BCP 47 language tag, such as es-ES, sw or zh-Hant-TW. Returns estimate_id, what is reused (existing translations: a published one is free; your own "
            "draft in the target project is published in the run, charged for publication only), what is copied (100 "
            "credits per draft, and 100 per component to publish it; the model's 500 covers its own), unanchored "
            "enumeration values (no definition IRI, so nothing ties the translated value to its original: show "
            "these to the person), the total, how it is funded, and refusals. Show the person the summary; "
            "translate_model needs the estimate_id. Free."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "ct_id of the published model or component."},
                "language": {"type": "string", "description": "Any BCP 47 language tag, such as es-ES, sw or zh-Hant-TW."},
                "project": {"type": "string", "description": "Optional target project ct_id; default: the Modeler's default project."},
                "review": {"type": "boolean", "default": False,
                           "description": "Drafts only, for a person to review in SDCStudio; nothing is published or charged for publication."},
            },
            "required": ["source", "language"],
        },
    },
    {
        "name": "translate_model",
        "description": (
            "Translate a published model or component into another language and publish it (review true: drafts "
            "only). Needs the estimate_id from estimate_translation with the same source, language, project and "
            "review, after the person has seen the total; when the person created the key with auto-spend in "
            "SDCStudio, no estimate_id is needed and the result carries the estimate. Waits for the job. Returns "
            "status completed (translated_ct_id, created, reused, unanchored, and a model's links), "
            "drafts_unpublished (made and charged, publishing failed: translate again to finish, charged for "
            "publication only), or in_flight (still running: poll job_status, never translate again). A 402 names "
            "the reason; report it and stop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "estimate_id": {"type": "string", "description": "From estimate_translation; not needed with an auto-spend key."},
                "source": {"type": "string"},
                "language": {"type": "string"},
                "project": {"type": "string"},
                "review": {"type": "boolean", "default": False},
            },
            "required": ["source", "language"],
        },
    },
    {
        "name": "job_status",
        "description": (
            "One job (job_ct_id from build_model, build_and_deliver or translate_model): status queued, running, completed or failed; step; "
            "cost_credits so far; on completion the model's links (dm_ct_id, catalog_url, package_url); "
            "on failure the reason. Poll every few seconds, backing off. Free."
        ),
        "inputSchema": {"type": "object", "properties": {"job_ct_id": {"type": "string"}}, "required": ["job_ct_id"]},
    },
    {
        "name": "download_package",
        "description": (
            "Download a model's package (the ZIP: schema, XML template and JSON instance of the "
            "Governed Data Record, JSON-LD, HTML, RDF, SHACL, GQL) into the server's output directory, "
            "with this key: it works for a model in a private project, such as the one build_model "
            "just made, where get_model and the public catalog do not. When the model is published "
            "and its package not yet built, it builds it first (free, once; it needs the right to "
            "publish the model); generate false only downloads. Free."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ct_id": {"type": "string"},
                "generate": {"type": "boolean", "default": True},
            },
            "required": ["ct_id"],
        },
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
        "name": "add_supabase_kit",
        "description": (
            "Write a published model's governed data kit into a Supabase project: typed TypeScript for the "
            "model's records, the gate (an Edge Function that validates every record against the published "
            "model before storing it) and the migrations. The first call writes the project base too; call "
            "again for each further model. Components already in the project are never rewritten, and each "
            "migration is written once, numbered after the project's latest. Then apply them with Supabase's "
            "own CLI or MCP server (supabase db push; supabase functions deploy gate). The first download of "
            "a model by this user, kit or application, costs 1,500 credits; later downloads of the same model "
            "are free. Needs confirm_charge true, after the person has agreed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ct_id": {"type": "string"},
                "project_dir": {
                    "type": "string",
                    "description": "The project holding supabase/, relative to the server's output directory. Default: the output directory.",
                },
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
                               project=_string(args, "project") or None, scope=_string(args, "scope") or None,
                               page=int(args.get("page") or 1),
                               page_size=int(args.get("page_size") or 25))


def _handle_list_models(args: dict) -> Any:
    return get_client().list_models(_string(args, "query"), page=int(args.get("page") or 1), page_size=int(args.get("page_size") or 25))


def _handle_get_model(args: dict) -> Any:
    return get_client().get_model(_string(args, "ct_id", required=True))


def _handle_estimate_model(args: dict) -> Any:
    tree = _object(args, "tree", required=True)
    title = _string(args, "title", required=True)
    if args.get("deliver") is not None:
        # The run's estimate is the answer: its estimate_id binds the deliverables on a server that prices them.
        plan = runs.plan(get_client(), tree, title, _deliver_arg(args))
        _plans[(plan["estimate_id"], tuple(plan["deliver"]))] = plan
        estimate = dict(plan["estimate"])
        estimate["run"] = {k: v for k, v in plan.items() if k != "estimate"}
        return estimate
    publish = args.get("publish", True)
    if not isinstance(publish, bool):
        raise ValueError("`publish` must be a boolean.")
    estimate = get_client().estimate(tree, title, publish=publish)
    _estimates[estimate["estimate_id"]] = estimate
    key = estimate.get("key") if estimate.get("source") == "server" else None
    if key and key.get("remaining_credits") is not None:
        estimate["key_remaining_credits"] = key["remaining_credits"]
        estimate["within_key_limit"] = bool(key.get("within_limit"))
    else:
        policy = _policy_or_none()
        if policy and policy.get("spend_limit_credits") is not None:
            remaining = max(0, policy["spend_limit_credits"] - policy.get("spent_credits", 0))
            estimate["key_remaining_credits"] = remaining
            estimate["within_key_limit"] = estimate["total_credits"] <= remaining
    return estimate


def _deliver_arg(args: dict) -> list[str]:
    value = args.get("deliver")
    if value is not None and not isinstance(value, list):
        raise ValueError("`deliver` must be an array of strings.")
    return runs.parse_deliver(value)


def _handle_build_and_deliver(args: dict) -> Any:
    estimate_id = _string(args, "estimate_id")
    title = _string(args, "title", required=True)
    tree = _object(args, "tree", required=True)
    items = _deliver_arg(args)
    # Plan now: the wallet and the key may have moved since the person saw the total, and the server
    # says whether this key runs with auto-spend (a setting only a signed-in person can make).
    current = runs.plan(get_client(), tree, title, items)
    shown = _plans.get((estimate_id, tuple(items))) if estimate_id else None
    if current["auto_spend"] and shown is None:
        if estimate_id and estimate_id != current["estimate_id"]:
            raise ValueError("The estimate_id does not match this title, tree and deliver; run estimate_model again.")
        project = _string(args, "project_dir", default="") or os.environ.get("SDCMODELER_PROJECT_DIR", "")
        result = runs.run(get_client(), tree, title, current, output_dir=_output_dir,
                          project_dir=(_output_dir / (project or ".")).resolve(), description=_string(args, "description"))
        result["authorized_by"] = "auto_spend"
        return _with_next(result)
    if shown is None:
        raise ValueError(
            "build_and_deliver needs the estimate_id that estimate_model returned with this exact title, tree and "
            f"deliver ({', '.join(items) or 'nothing'}) in this session; run estimate_model with deliver, show the "
            "person the run's total, then build."
        )
    project = _string(args, "project_dir", default="") or os.environ.get("SDCMODELER_PROJECT_DIR", "")
    project_dir = (_output_dir / (project or ".")).resolve()
    if current["estimate_id"] != estimate_id:
        raise ValueError("The title or the tree changed since the estimate; run estimate_model again.")
    if current["total_credits"] != shown["total_credits"]:
        raise ValueError(
            f"The run now costs {current['total_credits']} credits, not the {shown['total_credits']} the person saw; "
            "show them the new estimate."
        )
    result = runs.run(get_client(), tree, title, current, output_dir=_output_dir, project_dir=project_dir,
                      description=_string(args, "description"))
    result["authorized_by"] = "estimate_shown"
    return _with_next(result)


def _with_next(result: dict) -> dict:
    if result.get("status") == "in_flight":
        return result   # its own next: poll the job, never build again
    if result["failed"]:
        result["next"] = (
            "The model is published; do not build it again. Fetch what failed with its own tool "
            "(download_package, generate_app, add_supabase_kit) once the person agrees: the application or "
            "kit charge is made once per model, so it is free if the run already paid it, and 1,500 credits "
            "if it did not."
        )
    return result


def _policy_or_none() -> dict | None:
    try:
        return get_client().key_policy()
    except SDCStudioError:
        return None


def _problem_text(problem: Any) -> str:
    """A problem from the server's estimate is ``{path, ct_id?, reason}``; one from the local walk is a string (4.3.1)."""
    if isinstance(problem, dict):
        ct_id = f" ({problem['ct_id']})" if problem.get("ct_id") else ""
        return f"{problem.get('path', '')}{ct_id}: {problem.get('reason', '')}".strip(": ")
    return str(problem)


def _handle_build_model(args: dict) -> Any:
    estimate_id = _string(args, "estimate_id", required=True)
    title = _string(args, "title", required=True)
    tree = _object(args, "tree", required=True)
    publish = bool(args.get("publish", True))
    current = get_client().estimate(tree, title, publish=publish)
    shown = _estimates.get(estimate_id)
    if shown is None or current["estimate_id"] != estimate_id:
        raise ValueError(
            "build_model needs the estimate_id that estimate_model returned for this exact title and tree "
            "in this session; run estimate_model, show the person the cost, then build."
        )
    if current["problems"]:
        raise ValueError("The tree has problems: " + "; ".join(_problem_text(p) for p in current["problems"]))
    remaining = None
    if current.get("source") == "server" and (current.get("key") or {}).get("remaining_credits") is not None:
        remaining = current["key"]["remaining_credits"]
    else:
        policy = _policy_or_none()
        if policy and policy.get("spend_limit_credits") is not None:
            remaining = max(0, policy["spend_limit_credits"] - policy.get("spent_credits", 0))
    if remaining is not None and current["total_credits"] > remaining:
        raise ValueError(
            f"This key may still spend {remaining} credits and the build needs {current['total_credits']}. "
            "Ask the person for a key with a higher limit, or to run the build themselves."
        )
    generate = bool(args.get("generate", True)) and publish
    result = get_client().assemble(title, tree, description=_string(args, "description"),
                                   contextual=_object(args, "contextual"), estimate_id=estimate_id,
                                   publish=publish, generate=generate)
    result["estimate"] = current
    result["requested"] = {"publish": publish, "generate": generate}
    if result.get("mode") == "mixed":
        result["next"] = (
            f"Poll job_status with job_ct_id {result.get('job_ct_id')} until completed; its result then says "
            "published and generated and carries the model's links."
            + ("" if publish else " Its bench_url is where a person opens the draft on the Bench in SDCStudio "
               "to review and finish it; give the person that link.")
        )
    elif not publish and result.get("bench_url"):
        result["next"] = (
            f"The draft is in the key owner's current project. Give the person the link {result['bench_url']}: "
            "it opens the draft on the Bench in SDCStudio to review and finish."
        )
    return result


def _translation_args(args: dict) -> dict[str, Any]:
    review = args.get("review", False)
    if not isinstance(review, bool):
        raise ValueError("`review` must be a boolean.")
    return {"source": _string(args, "source", required=True).strip(),
            "language": _string(args, "language", required=True).strip(),
            "project": _string(args, "project").strip() or None,
            "publish": not review}


def _shown(the_plan: dict) -> dict:
    out = {k: v for k, v in the_plan.items() if k not in ("estimate", "request")}
    out["summary"] = translations.summary(the_plan)
    return out


def _handle_estimate_translation(args: dict) -> Any:
    a = _translation_args(args)
    the_plan = translations.plan(get_client(), a["source"], a["language"], project=a["project"], publish=a["publish"])
    if the_plan["estimate_id"]:
        _translations[the_plan["estimate_id"]] = the_plan
    out = _shown(the_plan)
    out["next"] = ("Show the person the summary, then translate_model with this estimate_id."
                   if not the_plan["refusals"] else "It cannot run as it stands; tell the person the refusals.")
    return out


def _handle_translate_model(args: dict) -> Any:
    a = _translation_args(args)
    estimate_id = _string(args, "estimate_id")
    current = translations.plan(get_client(), a["source"], a["language"], project=a["project"], publish=a["publish"])
    shown = _translations.get(estimate_id) if estimate_id else None
    if current["auto_spend"] and shown is None:
        if estimate_id and estimate_id != current["estimate_id"]:
            raise ValueError("The estimate_id does not match this source, language, project and review; run estimate_translation again.")
        result = translations.run(get_client(), current)
        result["authorized_by"] = "auto_spend"
    else:
        if shown is None:
            raise ValueError(
                "translate_model needs the estimate_id that estimate_translation returned for this source, language, "
                "project and review in this session; run estimate_translation, show the person the summary, then translate."
            )
        if current["estimate_id"] != estimate_id:
            raise ValueError("The source, language, project or review changed since the estimate; run estimate_translation again.")
        if current["total_credits"] != shown["total_credits"]:
            raise ValueError(f"The translation now costs {current['total_credits']} credits, not the {shown['total_credits']} "
                             "the person saw; show them the new estimate.")
        result = translations.run(get_client(), current)
        result["authorized_by"] = "estimate_shown"
    result["plan"] = _shown(result["plan"])
    return result


def _handle_job_status(args: dict) -> Any:
    return get_client().job_status(_string(args, "job_ct_id", required=True))


def _handle_download_package(args: dict) -> Any:
    ct_id = _string(args, "ct_id", required=True)
    generate = args.get("generate", True)
    if not isinstance(generate, bool):
        raise ValueError("`generate` must be a boolean.")
    c = get_client()
    built_now = False
    try:
        path = c.download_package(ct_id, _output_dir)
    except SDCStudioError as exc:
        # 404 until the package exists (and for a draft); a published model's package is built once, free.
        if exc.status != 404 or not generate:
            raise
        c.generate_package(ct_id)
        built_now = True
        path = c.download_package(ct_id, _output_dir)
    return {"path": str(path), "built_now": built_now}


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


def _handle_add_supabase_kit(args: dict) -> Any:
    ct_id = _string(args, "ct_id", required=True)
    project = (_output_dir / (_string(args, "project_dir", default="") or ".")).resolve()
    if not project.is_dir():
        raise ValueError(f"{project} is not a directory: name the project that holds (or will hold) supabase/")
    if not args.get("confirm_charge"):
        raise ValueError(
            f"add_supabase_kit downloads the model's kit, which costs {DOWNLOAD_APP_CREDITS} credits the first time "
            "this user downloads this model, kit or application (free after that). Tell the person, and call again "
            "with confirm_charge true."
        )
    kit = get_client().supabase_kit(ct_id)
    written = write_kit(project, kit)
    return {"project": str(project), "models": kit["models"], "written": written, "charged": kit.get("charged"),
            "next": "supabase db push; supabase functions deploy gate"}


def _handle_wallet(args: dict) -> Any:
    c = get_client()
    return {"wallet": c.wallet(), "key": c.key_policy()}


TOOL_HANDLERS = {
    "search_components": _handle_search_components,
    "list_models": _handle_list_models,
    "get_model": _handle_get_model,
    "estimate_model": _handle_estimate_model,
    "build_model": _handle_build_model,
    "build_and_deliver": _handle_build_and_deliver,
    "estimate_translation": _handle_estimate_translation,
    "translate_model": _handle_translate_model,
    "job_status": _handle_job_status,
    "download_package": _handle_download_package,
    "generate_app": _handle_generate_app,
    "add_supabase_kit": _handle_add_supabase_kit,
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
                "The modeler's client for SDCStudio. Search first and reuse: your own published components, "
                "then the public catalog, and mint last; estimate before you build and show the person the cost; build only with the "
                "estimate_id you showed; for a whole run (build, publish, then the package, application or kit), "
                "estimate_model with deliver, show the run's total, then build_and_deliver (a key the person "
                "made with auto-spend may run it without the estimate; the result carries it); prefer the model package over the generated application when the "
                "feature lives inside an existing application; fetch a model's package, including one you just built "
                "in a private project, with download_package; for a TypeScript app on Supabase, write the model's kit "
                "into the project with add_supabase_kit; to translate a published model or component into another "
                "language, estimate_translation, show its summary (the unanchored values above all), then translate_model. A 402 means the person acts at "
                f"{get_client().base_url}/app/settings?tab=wallet (fund it, or fix what its reason names); never try to pay. "
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
            if exc.reason:   # a run the server could not fund: it says why; nothing was published or charged
                return _tool_error(req_id, (
                    f"Not funded ({exc.reason}): {exc} "
                    + runs.not_funded_text(get_client(), exc.reason, exc.wallet_url) + " Stop here; never try to pay."
                ))
            return _tool_error(req_id, (
                f"Insufficient credits: this needs {exc.estimated_cost_credits} credits and the wallet has "
                f"{exc.balance_credits}. The person funds the wallet at {get_client().base_url}/app/settings?tab=wallet; stop here."
            ))
        except TranslationRefused as exc:
            successor = f" Its successor is {exc.successor}; translate that instead." if exc.successor else ""
            return _tool_error(req_id, f"Translation refused ({exc.error_type}): {exc}.{successor} Nothing was created or charged.")
        except KeyPolicyRefused as exc:
            return _tool_error(req_id, f"The API key refused this ({exc.error_type}): {exc}")
        except (AssemblyRefused, SDCStudioError, KitConflictError, ValueError) as exc:
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

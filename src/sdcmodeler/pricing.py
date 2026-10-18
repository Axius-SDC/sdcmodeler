"""
What a model costs, computed locally before any call that spends.

SDCStudio's prices (Terms of Service, section 6; ``docs/user-guides/wallet-billing.md``):
1,000 credits per dollar; minting a component 100 credits; assembling a model
500; downloading an application 1,500, once per user per model since 4.16.0;
settling a record 1,000. The estimate below resolves an assembly tree the way
the server does: a leaf with ``ct_id`` is reused and free, a leaf with ``label``
and ``data_type`` is minted and billable. The server's own estimate endpoint,
when it exists, confirms this; until then this is the number an agent shows
the person before it builds.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

CREDITS_PER_USD = 1000
MINT_COMPONENT_CREDITS = 100
ASSEMBLE_MODEL_CREDITS = 500
DOWNLOAD_APP_CREDITS = 1500
SETTLE_CREDITS = 1000


def _walk(node: dict[str, Any], path: str, reuse: list, mint: list, problems: list) -> None:
    for comp in node.get("components", []) or []:
        label = comp.get("label", "")
        if comp.get("ct_id"):
            reuse.append({"path": f"{path}/{label or comp['ct_id']}", "ct_id": comp["ct_id"], "label": label})
        elif label and comp.get("data_type"):
            mint.append({"path": f"{path}/{label}", "label": label, "data_type": comp["data_type"]})
        else:
            problems.append(f"{path}: a component needs ct_id (reuse) or label and data_type (mint): {comp!r}")
    for cluster in node.get("clusters", []) or []:
        name = cluster.get("label", "")
        if cluster.get("ct_id"):
            reuse.append({"path": f"{path}/{name or cluster['ct_id']}", "ct_id": cluster["ct_id"], "label": name, "cluster": True})
            continue
        if not name:
            problems.append(f"{path}: a cluster needs a label")
        _walk(cluster, f"{path}/{name}", reuse, mint, problems)


def estimate_tree(tree: dict[str, Any], title: str = "") -> dict[str, Any]:
    """
    Resolve an assembly tree into what is reused, what would be minted, and the cost in credits.

    Returns ``{"estimate_id", "reuse", "mint", "problems", "model_credits",
    "mint_credits", "total_credits", "mode"}``. ``estimate_id`` is the first
    twelve hex characters of the SHA-256 of the canonical tree and title; the
    MCP server's ``build_model`` requires it, so a build always follows the
    estimate the person saw.
    """
    reuse: list = []
    mint: list = []
    problems: list = []
    _walk(tree, tree.get("label", "root"), reuse, mint, problems)
    mint_credits = MINT_COMPONENT_CREDITS * len(mint)
    canonical = json.dumps({"title": title, "tree": tree}, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "estimate_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12],
        "title": title,
        "reuse": reuse,
        "mint": mint,
        "problems": problems,
        "model_credits": ASSEMBLE_MODEL_CREDITS,
        "mint_credits": mint_credits,
        "total_credits": ASSEMBLE_MODEL_CREDITS + mint_credits,
        "mode": "mixed" if mint else "reuse",
    }

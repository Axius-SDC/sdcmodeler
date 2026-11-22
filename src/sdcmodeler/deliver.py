"""
Build and deliver in one run: estimate the whole run, check it can be paid, build, wait, deliver.

Phase 1 of SDCStudio's build-and-deliver PRD (``design_docs/build-and-deliver-PRD.md``): composed
from the calls SDCStudio already answers, so it works against any 4.26 deployment. One estimate covers
the build and the deliverables, so a person approves the run once.

- ``plan()`` prices the run: the server's estimate of the build, plus 1,500 credits once when the
  deliverables hold an application or a kit (one charge covers both, per user per model), and checks
  the total against the wallet's balance and the key's remaining limit. Nothing is spent.
- ``run()`` builds and publishes (into the Modeler's private default project), waits on the job,
  then delivers each item in order. It refuses before the build when the plan says the run cannot be
  paid, so a model is never published for a deliverable the wallet or the key cannot cover.

The deliverables come from the caller, or from ``SDCMODELER_DELIVER`` (comma-separated; default
``package``). A balance that changes while the run is in flight is the one risk this composition
cannot close; SDCStudio's hold at the start of a run (the PRD's phase 3) closes it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sdcmodeler.client import SDCStudioClient, SDCStudioError
from sdcmodeler.kits import write_kit
from sdcmodeler.pricing import DOWNLOAD_APP_CREDITS

DELIVERABLES = ("package", "app:lightweight", "app:enterprise", "kit")
CHARGED = frozenset({"app:lightweight", "app:enterprise", "kit"})
ALIASES = {"app": "app:lightweight"}


class RunRefused(ValueError):
    """The run cannot be paid, or its input is wrong; nothing was built."""


def parse_deliver(value: str | list[str] | None = None) -> list[str]:
    """
    The deliverables, in delivery order, without repeats. ``None`` reads ``SDCMODELER_DELIVER``
    (default ``package``); an empty string or list means build only. ``app`` means ``app:lightweight``.
    """
    if value is None:
        value = os.environ.get("SDCMODELER_DELIVER", "package")
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[str] = []
    for raw in items:
        if not isinstance(raw, str):
            raise RunRefused(f"A deliverable must be a string, not {raw!r}.")
        item = ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if not item:
            continue
        if item not in DELIVERABLES:
            raise RunRefused(f"Unknown deliverable {raw!r}; choose from {', '.join(DELIVERABLES)}.")
        if item not in out:
            out.append(item)
    return sorted(out, key=DELIVERABLES.index)


def _remaining_on_key(client: SDCStudioClient, estimate: dict[str, Any]) -> int | None:
    key = estimate.get("key") if estimate.get("source") == "server" else None
    if key and key.get("remaining_credits") is not None:
        return int(key["remaining_credits"])
    try:
        policy = client.key_policy()
    except SDCStudioError:
        return None
    if policy and policy.get("spend_limit_credits") is not None:
        return max(0, int(policy["spend_limit_credits"]) - int(policy.get("spent_credits") or 0))
    return None


def _balance(client: SDCStudioClient, estimate: dict[str, Any]) -> int | None:
    wallet = estimate.get("wallet") if estimate.get("source") == "server" else None
    if wallet and wallet.get("balance_credits") is not None:
        return int(wallet["balance_credits"])
    if not client.api_key:
        return None
    try:
        return int(client.wallet().get("balance_credits"))
    except (SDCStudioError, TypeError, ValueError):
        return None


def _problem_text(problem: Any) -> str:
    if isinstance(problem, dict):
        ct_id = f" ({problem['ct_id']})" if problem.get("ct_id") else ""
        return f"{problem.get('path', '')}{ct_id}: {problem.get('reason', '')}".strip(": ")
    return str(problem)


def plan(client: SDCStudioClient, tree: dict[str, Any], title: str,
         deliver: str | list[str] | None = None) -> dict[str, Any]:
    """
    Price the whole run and say whether it can be paid. Spends nothing.

    Returns the build's estimate under ``estimate``, plus ``estimate_id`` (the build's, which the
    server checks), ``deliver``, ``deliver_credits``, ``build_credits``, ``total_credits``,
    ``balance_credits`` and ``key_remaining_credits`` (None when unknown or unlimited), and
    ``refusals``: why the run would be refused before anything is built (empty when it can run).
    """
    items = parse_deliver(deliver)
    estimate = client.estimate(tree, title)
    build_credits = int(estimate["total_credits"])
    # A run assembles a new model, so its first application or kit is always charged, once.
    deliver_credits = DOWNLOAD_APP_CREDITS if CHARGED.intersection(items) else 0
    total = build_credits + deliver_credits
    balance = _balance(client, estimate)
    remaining = _remaining_on_key(client, estimate)
    refusals = [f"The tree has a problem: {_problem_text(p)}" for p in estimate.get("problems") or []]
    if not client.api_key:
        refusals.append("Building needs SDCSTUDIO_API_KEY.")
    if balance is not None and total > balance:
        refusals.append(f"The run needs {total} credits and the wallet has {balance}.")
    if remaining is not None and total > remaining:
        refusals.append(f"The run needs {total} credits and this key may still spend {remaining}.")
    return {
        "estimate_id": estimate["estimate_id"],
        "title": estimate.get("title", title),
        "deliver": items,
        "build_credits": build_credits,
        "deliver_credits": deliver_credits,
        "total_credits": total,
        "balance_credits": balance,
        "key_remaining_credits": remaining,
        "publishes_into": "the Modeler's private default project",
        "refusals": refusals,
        "estimate": estimate,
    }


def _model_ct_id(build: dict[str, Any]) -> str | None:
    if build.get("mode") == "reuse":
        return build.get("ct_id") or build.get("dm_ct_id")
    result = (build.get("job") or {}).get("result") or {}
    return result.get("dm_ct_id") or result.get("ct_id")


def _deliver_package(client: SDCStudioClient, ct_id: str, output_dir: Path) -> dict[str, Any]:
    built_now = False
    try:
        path = client.download_package(ct_id, output_dir)
    except SDCStudioError as exc:
        if exc.status != 404:
            raise
        client.generate_package(ct_id)
        built_now = True
        path = client.download_package(ct_id, output_dir)
    return {"path": str(path), "built_now": built_now}


def _deliver_app(client: SDCStudioClient, ct_id: str, profile: str, output_dir: Path) -> dict[str, Any]:
    client.generate_app(ct_id, profile)
    path, charged = client.download_app(ct_id, output_dir, profile)
    return {"path": str(path), "charged": charged, "profile": profile}


def _deliver_kit(client: SDCStudioClient, ct_id: str, project_dir: Path) -> dict[str, Any]:
    kit = client.supabase_kit(ct_id)
    return {"project": str(project_dir), "written": write_kit(project_dir, kit), "charged": kit.get("charged"),
            "next": "supabase db push; supabase functions deploy gate"}


def run(client: SDCStudioClient, tree: dict[str, Any], title: str, the_plan: dict[str, Any], *,
        output_dir: str | Path = ".", project_dir: str | Path | None = None, description: str = "",
        timeout: float = 900.0) -> dict[str, Any]:
    """
    Build, publish and deliver what ``the_plan`` priced. Refuses, before anything is built, when the plan
    has refusals or the tree or title changed since it was made (the server would refuse a stale
    ``estimate_id`` too).

    Returns ``{"ct_id", "build", "delivered": {item: result}, "failed": {item: reason}, "plan"}``. Once
    the model is published, a deliverable that fails is reported under ``failed`` and the others still
    run: the model stays published and the failed item can be fetched again with its own command.
    """
    if the_plan.get("refusals"):
        raise RunRefused("Nothing was built. " + " ".join(the_plan["refusals"]))
    output_dir = Path(output_dir).resolve()
    kit_dir = Path(project_dir).resolve() if project_dir is not None else output_dir
    if "kit" in the_plan["deliver"] and not kit_dir.is_dir():
        raise RunRefused(f"Nothing was built. {kit_dir} is not a directory: name the project that holds (or will hold) supabase/.")
    current = client.estimate(tree, title)
    if current["estimate_id"] != the_plan["estimate_id"]:
        raise RunRefused("Nothing was built. The title or the tree changed since the plan; plan again and show the person the new total.")

    build = client.assemble(title, tree, description=description, estimate_id=the_plan["estimate_id"],
                            publish=True, generate=True)
    if build.get("mode") == "mixed" and build.get("job_ct_id"):
        build["job"] = client.wait_for_job(build["job_ct_id"], timeout=timeout)
        if build["job"].get("status") != "completed":
            reason = build["job"].get("error") or build["job"].get("status")
            raise RunRefused(f"The build job {build['job_ct_id']} did not complete ({reason}); nothing was delivered.")
    ct_id = _model_ct_id(build)
    out: dict[str, Any] = {"ct_id": ct_id, "build": build, "delivered": {}, "failed": {}, "plan": the_plan}
    if not ct_id:
        out["failed"] = {item: "the build's result named no model" for item in the_plan["deliver"]}
        return out
    for item in the_plan["deliver"]:
        try:
            if item == "package":
                out["delivered"][item] = _deliver_package(client, ct_id, output_dir)
            elif item.startswith("app:"):
                out["delivered"][item] = _deliver_app(client, ct_id, item.split(":", 1)[1], output_dir)
            elif item == "kit":
                out["delivered"][item] = _deliver_kit(client, ct_id, kit_dir)
        except Exception as exc:  # the model is published; report the item and go on
            out["failed"][item] = str(exc) or type(exc).__name__
    return out

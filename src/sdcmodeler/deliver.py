"""
Build and deliver in one run: estimate the whole run, check it can be paid, build, wait, deliver.

SDCStudio's build-and-deliver PRD (``design_docs/build-and-deliver-PRD.md``). One estimate covers the
build and the deliverables, so a person approves the run once. Two ways to run it, chosen by what the
deployment answers, never by a setting:

- **On the server** (``mode: "server"``, SDCStudio with the build-and-deliver release; phase 4 here).
  The estimate prices the deliverables and says how the run is funded; ``assemble/`` funds and holds the
  whole run (a reload included, when the person allows one), publishes, and delivers each item from the
  hold. This module then downloads what the server made ready and writes the kit.
- **Composed** (``mode: "composed"``, older deployments; phase 1). This module adds the deliverable charge
  to the build's estimate, checks the total against the balance and the key, builds, waits, and fetches
  each deliverable itself.

The server is the authority on money: on the server path a short balance is refused only when the
estimate says no reload can cover it, and a 402 from ``assemble/`` (raised as ``InsufficientCredits``,
with its ``reason``) means nothing was published and nothing charged.

The deliverables come from the caller, or from ``SDCMODELER_DELIVER`` (comma-separated; default
``package``).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from sdcmodeler.client import SDCStudioClient, SDCStudioError
from sdcmodeler.kits import write_kit
from sdcmodeler.pricing import CHARGED_DELIVERABLES as CHARGED
from sdcmodeler.pricing import DOWNLOAD_APP_CREDITS

DELIVERABLES = ("package", "app:lightweight", "app:enterprise", "kit")
ALIASES = {"app": "app:lightweight"}


#: What the person does about each reason a run was not funded (SDCStudio's build-and-deliver release).
REASON_ACTIONS = {
    "auto_reload_off": "(fund the wallet, or turn auto-reload on).",
    "no_card": "(fund the wallet, or save a card for auto-reload).",
    "auto_reload_unconfirmed": "(confirm auto-reload in the wallet while signed in, so a key may trigger it).",
    "ceiling": "(raise the monthly auto-reload maximum, or fund the wallet).",
    "declined": "(update the card, or fund the wallet another way).",
    "organization_wallet": "(an organization admin adds credit; organization wallets never auto-reload).",
    "reload_failed": "(fund the wallet, or try again later).",
    "insufficient_funds": "(fund the wallet).",
}


def wallet_page(client: SDCStudioClient, wallet_url: str | None = None) -> str:
    """The wallet page on this deployment: the server's ``wallet_url`` (relative) when it gave one."""
    return client.base_url + (wallet_url or "/app/settings?tab=wallet")


def not_funded_text(client: SDCStudioClient, reason: str, wallet_url: str | None = None) -> str:
    """Where the person acts and what they do, for a run that cannot be funded."""
    action = REASON_ACTIONS.get(reason, REASON_ACTIONS["insufficient_funds"])
    return f"The person acts at {wallet_page(client, wallet_url)} {action}"


class RunRefused(ValueError):
    """The run cannot be paid, or its input is wrong; nothing was built."""


def parse_deliver(value: str | list[str] | None = None) -> list[str]:
    """
    The deliverables in SDCStudio's order (package, app:lightweight, app:enterprise, kit), without repeats.
    ``None`` reads ``SDCMODELER_DELIVER`` (default ``package``); an empty string or list means build only.
    ``app`` means ``app:lightweight``. The result is what the server and the ``estimate_id`` expect.
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


def _server_runs(estimate: dict[str, Any]) -> bool:
    """The one detection point: a deployment with the build-and-deliver release prices the run."""
    return estimate.get("source") == "server" and "deliver_credits" in estimate and "funding" in estimate


def _remaining_on_key(client: SDCStudioClient, estimate: dict[str, Any]) -> int | None:
    """What the key may still charge, counting what its active runs hold; None when unlimited or unknown."""
    key = estimate.get("key") if estimate.get("source") == "server" else None
    if key and key.get("remaining_credits") is not None:
        return int(key["remaining_credits"])
    try:
        policy = client.key_policy()
    except SDCStudioError:
        return None
    if policy and policy.get("spend_limit_credits") is not None:
        spoken_for = int(policy.get("spent_credits") or 0) + int(policy.get("reserved_credits") or 0)
        return max(0, int(policy["spend_limit_credits"]) - spoken_for)
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


def _funding_refusal(client: SDCStudioClient, funding: dict[str, Any], total: int) -> str | None:
    """Why the server says this run cannot be funded, or None when the balance or a reload covers it."""
    if funding.get("covered"):
        return None
    reload = funding.get("auto_reload") or {}
    if reload.get("would_cover"):
        return None
    reason = funding.get("blocked_reason") or "insufficient_funds"
    return (f"The run needs {total} credits, {funding.get('shortfall_credits')} more than the wallet holds, "
            f"and auto-reload cannot cover it ({reason}). " + not_funded_text(client, reason))


def plan(client: SDCStudioClient, tree: dict[str, Any], title: str,
         deliver: str | list[str] | None = None) -> dict[str, Any]:
    """
    Price the whole run and say whether it can be paid. Spends nothing.

    Returns ``mode`` (``server`` or ``composed``), ``estimate_id`` (the id ``assemble/`` will check),
    ``deliver``, ``build_credits``, ``deliver_credits``, ``total_credits``, ``balance_credits``,
    ``key_remaining_credits`` (None when unlimited or unknown), ``auto_spend`` (from the server; False
    when it does not say), ``funding`` and ``will_publish`` (server mode), ``refusals`` (why the run
    would be refused before anything is built; empty when it can run), and the raw ``estimate``.
    """
    items = parse_deliver(deliver)
    estimate = client.estimate(tree, title, deliver=items)
    server = _server_runs(estimate)
    if server:
        build_credits = int(estimate["build_credits"])
        deliver_credits = int(estimate["deliver_credits"])
        total = int(estimate["total_credits"])
    else:
        # An older deployment priced the build alone (and its id binds the build alone).
        build_credits = int(estimate.get("build_credits", estimate["total_credits"]))
        deliver_credits = DOWNLOAD_APP_CREDITS if CHARGED.intersection(items) else 0
        total = build_credits + deliver_credits
    remaining = _remaining_on_key(client, estimate)
    refusals = [f"The tree has a problem: {_problem_text(p)}" for p in estimate.get("problems") or []]
    if not client.api_key:
        refusals.append("Building needs SDCSTUDIO_API_KEY.")
    if remaining is not None and total > remaining:
        refusals.append(f"The run needs {total} credits and this key may still spend {remaining}.")
    funding = estimate.get("funding") if server else None
    balance = (funding or {}).get("balance_credits") if server else _balance(client, estimate)
    if server and client.api_key:
        refusal = _funding_refusal(client, funding or {}, total)
        if refusal:
            refusals.append(refusal)
    elif balance is not None and total > balance:
        refusals.append(f"The run needs {total} credits and the wallet has {balance}. "
                        + not_funded_text(client, "insufficient_funds"))
    return {
        "mode": "server" if server else "composed",
        "estimate_id": estimate["estimate_id"],
        "title": estimate.get("title", title),
        "deliver": items,
        "build_credits": build_credits,
        "deliver_credits": deliver_credits,
        "total_credits": total,
        "balance_credits": balance,
        "key_remaining_credits": remaining,
        "auto_spend": bool(estimate.get("auto_spend")) if server else False,
        "funding": funding,
        "will_publish": estimate.get("will_publish") if server else None,
        "publishes_into": "the Modeler's private default project",
        "refusals": refusals,
        "estimate": estimate,
    }


def _download_package(client: SDCStudioClient, ct_id: str, output_dir: Path, *, build_missing: bool) -> dict[str, Any]:
    built_now = False
    try:
        path = client.download_package(ct_id, output_dir)
    except SDCStudioError as exc:
        if exc.status != 404 or not build_missing:
            raise
        client.generate_package(ct_id)
        built_now = True
        path = client.download_package(ct_id, output_dir)
    return {"path": str(path), "built_now": built_now}


def _fetch(client: SDCStudioClient, ct_id: str, item: str, output_dir: Path, kit_dir: Path, *,
           generate: bool) -> dict[str, Any]:
    """One deliverable. ``generate`` makes the application first (composed path); the server already made it."""
    if item == "package":
        return _download_package(client, ct_id, output_dir, build_missing=generate)
    if item.startswith("app:"):
        profile = item.split(":", 1)[1]
        if generate:
            client.generate_app(ct_id, profile)
        path, charged = client.download_app(ct_id, output_dir, profile)
        return {"path": str(path), "charged": charged, "profile": profile}
    if item == "kit":
        kit = client.supabase_kit(ct_id)
        return {"project": str(kit_dir), "written": write_kit(kit_dir, kit), "charged": kit.get("charged"),
                "next": "supabase db push; supabase functions deploy gate"}
    raise RunRefused(f"Unknown deliverable {item}")


def _model_ct_id(build: dict[str, Any]) -> str | None:
    if build.get("mode") == "reuse":
        return build.get("ct_id") or build.get("dm_ct_id")
    result = (build.get("job") or {}).get("result") or {}
    return result.get("dm_ct_id") or result.get("ct_id")


def run(client: SDCStudioClient, tree: dict[str, Any], title: str, the_plan: dict[str, Any], *,
        output_dir: str | Path = ".", project_dir: str | Path | None = None, description: str = "",
        timeout: float = 900.0) -> dict[str, Any]:
    """
    Build, publish and deliver what ``the_plan`` priced. Refuses, before anything is built, when the plan
    has refusals or the tree, title or deliverables changed since it was made.

    Returns ``{"ct_id", "mode", "build", "delivered": {item: result}, "failed": {item: reason}, "plan"}``.
    Once the model is published, a deliverable that fails is reported under ``failed`` and the others
    still run; the model stays published. On the server path a failed item was not charged for, and is
    not fetched again here: fetching an application or kit outside the run would be charged outside it.
    """
    if the_plan.get("refusals"):
        raise RunRefused("Nothing was built. " + " ".join(the_plan["refusals"]))
    output_dir = Path(output_dir).resolve()
    kit_dir = Path(project_dir).resolve() if project_dir is not None else output_dir
    if "kit" in the_plan["deliver"] and not kit_dir.is_dir():
        raise RunRefused(f"Nothing was built. {kit_dir} is not a directory: name the project that holds (or will hold) supabase/.")
    server = the_plan.get("mode") == "server"
    current = client.estimate(tree, title, deliver=the_plan["deliver"])
    if current["estimate_id"] != the_plan["estimate_id"]:
        raise RunRefused("Nothing was built. The title, the tree or the deliverables changed since the plan; "
                         "plan again and show the person the new total.")

    build = client.assemble(title, tree, description=description, estimate_id=the_plan["estimate_id"],
                            publish=True, generate=True, deliver=the_plan["deliver"] if server else None)
    if build.get("mode") == "mixed" and build.get("job_ct_id"):
        try:
            build["job"] = client.wait_for_job(build["job_ct_id"], timeout=timeout)
        except (TimeoutError, SDCStudioError, httpx.HTTPError) as exc:
            # assemble/ accepted and funded the run; the server finishes it whether or not we are watching.
            # Say so plainly, or a caller (an agent with auto-spend above all) builds and pays a second time.
            return {"ct_id": None, "mode": the_plan.get("mode"), "status": "in_flight", "job_ct_id": build["job_ct_id"],
                    "build": build, "delivered": {}, "failed": {}, "plan": the_plan,
                    "reason": f"stopped waiting: {exc or type(exc).__name__}",
                    "next": (f"The run is funded and in flight on the server. Do not build again. Poll job_status with "
                             f"{build['job_ct_id']} (CLI: sdcmodeler job {build['job_ct_id']}); when it completes, its result "
                             "names the model and lists what was delivered; fetch the ready items with download_package, "
                             "generate_app or add_supabase_kit, already paid for by the run.")}
    ct_id = _model_ct_id(build)
    job = build.get("job") or {}
    published = build.get("published") if build.get("mode") == "reuse" else (job.get("result") or {}).get("published")
    if not ct_id or published is False:
        # Nothing to deliver from. A failed job that named a published model goes on below: its
        # deliveries are what failed, and saying "nothing was delivered" would invite a rebuild.
        reason = job.get("error") or job.get("status") or "the build named no published model"
        raise RunRefused(f"The build did not publish a model ({reason}); nothing was delivered.")
    out: dict[str, Any] = {"ct_id": ct_id, "mode": the_plan.get("mode"), "build": build, "delivered": {},
                           "failed": {}, "plan": the_plan}

    if server:
        reported = build.get("delivered") if build.get("mode") == "reuse" else (job.get("result") or {}).get("delivered")
        by_item = {d.get("item"): d for d in reported or []}
        out["deliver_charged_credits"] = (build if build.get("mode") == "reuse" else job.get("result") or {}).get("deliver_charged_credits")
        for item in the_plan["deliver"]:
            entry = by_item.get(item)
            if entry is None:
                out["failed"][item] = (job.get("result") or {}).get("finalize_error") or "the server did not deliver it"
            elif entry.get("status") != "ready":
                out["failed"][item] = entry.get("error") or "not delivered"
            else:
                try:
                    out["delivered"][item] = _fetch(client, ct_id, item, output_dir, kit_dir, generate=False)
                except Exception as exc:  # made and paid for on the server; the download can be retried free
                    out["failed"][item] = f"ready on the server, but the download failed: {exc or type(exc).__name__}"
        return out

    for item in the_plan["deliver"]:
        try:
            out["delivered"][item] = _fetch(client, ct_id, item, output_dir, kit_dir, generate=True)
        except Exception as exc:  # the model is published; report the item and go on
            out["failed"][item] = str(exc) or type(exc).__name__
    return out

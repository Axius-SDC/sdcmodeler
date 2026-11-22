"""
Translate a published model or component into another language, and publish it, in one run.

SDCStudio's translate PRD (``design_docs/translate-and-publish-charge-PRD.md``, phase 3; SDCStudio 4.28.0).
SDCStudio copies every component the source needs into the target language, each keeping its link to its
original (``prov:wasDerivedFrom``), its semantic links and its enumeration definition IRIs, and has the LLM
translate the human text of the model's data tree: labels, descriptions, enumeration values, boolean values,
ordinal symbols, the model's title and description. The contextual components (audit, attestation, access
control, protocol, workflow, parties) and Units stay in their original language; links, constraints and
definition IRIs never change. An existing
translation, published or one of the caller's own drafts in the target project, is reused, not copied again.

As for build and deliver, one estimate covers the run and the server is the authority on money: it funds and
holds the whole run before anything is created (a reload included, when the person allows one), and a 402
(``InsufficientCredits`` with its ``reason``) means nothing was created and nothing charged.

There is no local estimate and no composed fallback: a deployment without the endpoint says so.
"""
from __future__ import annotations

from typing import Any

import httpx

from sdcmodeler.client import SDCStudioClient, SDCStudioError
from sdcmodeler.deliver import RunRefused, _funding_refusal, _problem_text, _remaining_on_key

MODEL_LINKS = ("dm_ct_id", "model_url", "catalog_url", "package_url")


def plan(client: SDCStudioClient, source: str, language: str, *, project: str | None = None,
         publish: bool = True) -> dict[str, Any]:
    """
    Price the translation and say whether it can run. Spends nothing.

    Returns ``estimate_id``, ``source``, ``language``, ``project``, ``publish``, ``reuse``, ``copy``,
    ``unanchored`` (enumeration values that will have nothing tying them to their original once translated),
    ``model_credits``, ``draft_credits``, ``publish_credits``, ``total_credits``, ``funding``, ``auto_spend``,
    ``key_remaining_credits``, ``refusals`` (why it would be refused before anything is created; empty when
    it can run) and the raw ``estimate``.
    """
    if not isinstance(source, str) or not source.strip():
        raise RunRefused("Name the published model or component to translate (its ct_id).")
    if not isinstance(language, str) or not language.strip():
        raise RunRefused("Name the target language, e.g. pt-BR.")
    if not client.api_key:
        raise RunRefused("Translating needs SDCSTUDIO_API_KEY.")
    estimate = client.translate_estimate(source.strip(), language.strip(), project=project or None, publish=publish)
    total = int(estimate.get("total_credits") or 0)
    refusals = [f"{_problem_text(p)}" + (f" Translate its successor {p['successor']} instead." if isinstance(p, dict) and p.get("successor") else "")
                for p in estimate.get("problems") or []]
    remaining = _remaining_on_key(client, estimate)
    if estimate.get("within_key_limit") is False or (remaining is not None and total > remaining):
        refusals.append(f"The translation needs {total} credits and this key may still spend "
                        f"{remaining if remaining is not None else 'less'}.")
    funding = estimate.get("funding")
    if funding:
        refusal = _funding_refusal(client, funding, total)
        if refusal:
            refusals.append(refusal.replace("The run needs", "The translation needs", 1))
    return {
        # A refused source (same language, retired) answers its problems and no id; the refusals say why.
        "estimate_id": estimate.get("estimate_id"),
        "source": estimate.get("source"),
        "language": estimate.get("language", language),
        "project": estimate.get("project"),
        "publish": bool(publish),
        "reuse": estimate.get("reuse") or [],
        "copy": estimate.get("copy") or [],
        "unanchored": estimate.get("unanchored") or [],
        "model_credits": int(estimate.get("model_credits") or 0),
        "draft_credits": int(estimate.get("draft_credits") or 0),
        "publish_credits": int(estimate.get("publish_credits") or 0),
        "total_credits": total,
        "funding": funding,
        "auto_spend": bool(estimate.get("auto_spend")),
        "key_remaining_credits": remaining,
        "refusals": refusals,
        "estimate": estimate,
        # What the plan was made with, so run() asks for exactly this.
        "request": {"source": source.strip(), "language": language.strip(), "project": project or None,
                    "publish": bool(publish)},
    }


def summary(the_plan: dict[str, Any]) -> str:
    """One paragraph for the person: what is translated, reused and flagged, and what it costs."""
    src = the_plan.get("source") or {}
    proj = the_plan.get("project") or {}
    published = sum(1 for r in the_plan["reuse"] if r.get("published", True))
    drafts = len(the_plan["reuse"]) - published
    where = proj.get("name") or "the default project"
    if proj.get("public"):
        where += " (public)"
    text = (f"Translate {src.get('kind', 'source')} {src.get('label') or the_plan['request']['source']} "
            f"({src.get('lang', '?')}) into {the_plan['language']} in {where}: "
            f"{len(the_plan['copy'])} new draft(s), {published} published translation(s) reused free"
            + (f", {drafts} of your draft(s) reused and published" if drafts else "") + "; "
            f"{the_plan['total_credits']} credits in all"
            f" ({the_plan['model_credits']} model, {the_plan['draft_credits']} drafts, {the_plan['publish_credits']} publication). ")
    text += ("Publishing is permanent." if the_plan["publish"] else "Drafts only, for review; nothing is published.")
    if the_plan["unanchored"]:
        text += (f" {len(the_plan['unanchored'])} enumeration value(s) have no definition IRI, so once translated nothing "
                 "ties them to the original: " + ", ".join(str(u.get("value")) for u in the_plan["unanchored"][:10]) + ".")
    return text


def run(client: SDCStudioClient, the_plan: dict[str, Any], *, timeout: float = 900.0) -> dict[str, Any]:
    """
    Translate (and publish, when the plan says so) what ``the_plan`` priced. Refuses, before anything is created,
    when the plan has refusals or the estimate changed since it was made.

    Returns ``status``:

    - ``completed``: ``translated_ct_id``, ``created``, ``reused``, ``unanchored``, ``published``, ``generated``
      and, for a model, its links.
    - ``drafts_unpublished``: the translation was made and charged but publishing failed; the drafts stay.
      Translating again reuses them (charged for publication only), or the person publishes them in the app.
    - ``in_flight``: the wait ended while the server was still working; it is funded and will finish.

    A translation that fails outright raises ``RunRefused``: nothing was created and the hold was released.
    """
    if the_plan.get("refusals"):
        raise RunRefused("Nothing was translated. " + " ".join(the_plan["refusals"]))
    req = the_plan["request"]
    current = client.translate_estimate(req["source"], req["language"], project=req["project"], publish=req["publish"])
    if current["estimate_id"] != the_plan["estimate_id"]:
        raise RunRefused("Nothing was translated. The source, language, project or publish changed since the estimate; "
                         "estimate again and show the person the new total.")
    if int(current.get("total_credits") or 0) != the_plan["total_credits"]:
        raise RunRefused(f"Nothing was translated. It now costs {current.get('total_credits')} credits, not the "
                         f"{the_plan['total_credits']} the person saw; show them the new estimate.")

    started = client.translate(req["source"], req["language"], project=req["project"], publish=req["publish"],
                               estimate_id=the_plan["estimate_id"])
    job_ct_id = started.get("job_ct_id")
    base = {"job_ct_id": job_ct_id, "language": the_plan["language"], "plan": the_plan}
    if not job_ct_id:
        raise RunRefused(f"The server did not start a translation job: {started!r}")
    try:
        job = client.wait_for_job(job_ct_id, timeout=timeout)
    except (TimeoutError, SDCStudioError, httpx.HTTPError) as exc:
        return {**base, "status": "in_flight", "reason": f"stopped waiting: {exc or type(exc).__name__}",
                "next": (f"The translation is funded and in flight on the server. Do not translate again. Poll job_status "
                         f"with {job_ct_id} (CLI: sdcmodeler job {job_ct_id}); its result names the translated model "
                         "or component when it completes.")}
    result = job.get("result") or {}
    out = {**base, "job": job,
           "translated_ct_id": result.get("translated_ct_id"),
           "created": result.get("created") or [],
           "reused": result.get("reused") or [],
           "unanchored": result.get("unanchored") or [],
           "published": result.get("published"),
           "generated": result.get("generated")}
    for link in MODEL_LINKS:
        if result.get(link):
            out[link] = result[link]
    if job.get("status") == "completed":
        out["status"] = "completed"
        return out
    if result.get("translated_ct_id") or result.get("created"):
        out["status"] = "drafts_unpublished"
        out["reason"] = result.get("finalize_error") or job.get("error") or "publishing failed"
        out["next"] = ("The translation was made and charged; publishing failed and the drafts stay in the project. "
                       "Translate again to finish (the drafts are reused, charged for publication only), or the person "
                       "publishes them in the app.")
        return out
    raise RunRefused(f"The translation failed ({job.get('error') or result.get('error') or 'no reason given'}); "
                     "nothing was created and the hold was released.")

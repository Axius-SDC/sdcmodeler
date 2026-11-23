"""``sdcmodeler``: the client at a terminal. ``sdcmodeler mcp`` starts the MCP server."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sdcmodeler import __version__
from sdcmodeler.client import SDCStudioClient, SDCStudioError
from sdcmodeler import deliver as runs
from sdcmodeler import translate as translations
from sdcmodeler.kits import KitConflictError, write_kit


def _print(data) -> None:
    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def _load_tree(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ship(c, a):
    tree = _load_tree(a.tree)
    plan = runs.plan(c, tree, a.title, a.deliver)
    shown = {k: v for k, v in plan.items() if k != "estimate"}
    if a.plan:
        return shown
    if plan["refusals"]:
        raise runs.RunRefused("Nothing was built. " + " ".join(plan["refusals"]))
    authorized_by = "yes_flag" if a.yes else "prompt"
    if not a.yes:
        sys.stderr.write(
            f"{plan['title']}: {plan['build_credits']} credits to build and publish into {plan['publishes_into']}"
            + (f", {plan['deliver_credits']} for {', '.join(i for i in plan['deliver'] if i in runs.CHARGED)}" if plan["deliver_credits"] else "")
            + f"; {plan['total_credits']} in all. Deliver: {', '.join(plan['deliver']) or 'nothing'}.\n"
            + _funding_line(plan)
            + "Publishing is permanent. Proceed? [y/N] "
        )
        sys.stderr.flush()
        if not sys.stdin.isatty() or sys.stdin.readline().strip().lower() not in ("y", "yes"):
            raise runs.RunRefused("Not approved; nothing was built. Use --yes to approve without the prompt.")
    out_dir = a.dir or os.environ.get("SDCMODELER_OUTPUT_DIR") or "."
    project = a.project or os.environ.get("SDCMODELER_PROJECT_DIR") or out_dir
    result = runs.run(c, tree, a.title, plan, output_dir=out_dir, project_dir=project, description=a.description)
    result["authorized_by"] = authorized_by
    return result


def _translate(c, a):
    plan = translations.plan(c, a.ct_id, a.language, project=a.project, publish=not a.review)
    shown = {k: v for k, v in plan.items() if k not in ("estimate", "request")}
    shown["summary"] = translations.summary(plan)
    if a.plan:
        return shown
    if plan["refusals"]:
        raise runs.RunRefused("Nothing was translated. " + " ".join(plan["refusals"]))
    authorized_by = "yes_flag" if a.yes else "prompt"
    if not a.yes:
        sys.stderr.write(shown["summary"] + "\n" + _funding_line(plan) + "Proceed? [y/N] ")
        sys.stderr.flush()
        if not sys.stdin.isatty() or sys.stdin.readline().strip().lower() not in ("y", "yes"):
            raise runs.RunRefused("Not approved; nothing was translated. Use --yes to approve without the prompt.")
    result = translations.run(c, plan)
    result["plan"] = shown
    result["authorized_by"] = authorized_by
    return result


def _funding_line(plan) -> str:
    reload = (plan.get("funding") or {}).get("auto_reload") or {}
    # The translate estimate says covered: true when a reload covers the shortfall; the shortfall is what counts.
    if plan.get("funding") and (plan["funding"].get("shortfall_credits") or 0) > 0 and reload.get("would_cover"):
        return (f"The wallet is {plan['funding'].get('shortfall_credits')} credits short: auto-reload will add "
                f"${reload.get('reload_total_usd')} ({reload.get('reloads_needed')} reload(s)) before anything is published.\n")
    return ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sdcmodeler",
        description="The modeler's client for SDCStudio: search, estimate, build, generate; `sdcmodeler mcp` runs the MCP server.",
        epilog="The key is read from $SDCSTUDIO_API_KEY, the deployment from $SDCSTUDIO_URL.",
    )
    p.add_argument("--version", action="version", version=f"sdcmodeler {__version__}")
    p.add_argument("--url", default=None, help="SDCStudio base URL.")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("search", help="search the public catalog of published components")
    s.add_argument("query"); s.add_argument("--type"); s.add_argument("--project"); s.add_argument("--page-size", type=int, default=25)
    s.add_argument("--scope", choices=["mine", "public"], default=None,
                   help="mine: your own published components first, then public (needs a key; the default with one); public: the catalog only")
    s.set_defaults(func=lambda c, a: c.search_components(a.query, type=a.type, project=a.project, scope=a.scope, page_size=a.page_size))

    s = sub.add_parser("models", help="published models in the public catalog")
    s.add_argument("query", nargs="?", default="")
    s.set_defaults(func=lambda c, a: c.list_models(a.query))

    s = sub.add_parser("model", help="one published model, with its artifact routes")
    s.add_argument("ct_id")
    s.set_defaults(func=lambda c, a: c.get_model(a.ct_id))

    s = sub.add_parser("artifact", help="download one artifact of a published model (xsd, xml, json, jsonld, html, ttl, shacl, gql, zip)")
    s.add_argument("ct_id"); s.add_argument("kind"); s.add_argument("--dir", default=".")
    s.set_defaults(func=lambda c, a: {"path": str(c.download_artifact(a.ct_id, a.kind, a.dir))})

    s = sub.add_parser("estimate", help="what an assembly tree would reuse, mint and cost (no call that spends)")
    s.add_argument("tree", help="JSON file: {label, components: [...], clusters: [...]}"); s.add_argument("--title", default="")
    s.add_argument("--draft", action="store_true", help="price a draft build (build --draft): no publication charges; same estimate id")
    s.set_defaults(func=lambda c, a: c.estimate(_load_tree(a.tree), a.title, publish=not a.draft))

    s = sub.add_parser("build", help="assemble (and, with every leaf existing, publish and generate) a model; spends credits")
    s.add_argument("tree"); s.add_argument("--title", required=True); s.add_argument("--description", default="")
    s.add_argument("--estimate-id", default=None, help="the id `estimate` printed; the server refuses a build whose tree changed since")
    s.add_argument("--draft", action="store_true", help="do not publish (nothing is generated either)")
    s.add_argument("--no-generate", action="store_true", help="publish without building the package")
    s.add_argument("--wait", action="store_true", help="on the mixed path, poll the job until it completes or fails")
    def _build(c, a):
        result = c.assemble(a.title, _load_tree(a.tree), description=a.description, estimate_id=a.estimate_id,
                            publish=not a.draft, generate=not a.no_generate)
        if a.wait and result.get("mode") == "mixed" and result.get("job_ct_id"):
            result["job"] = c.wait_for_job(result["job_ct_id"])
        if a.draft:
            bench = result.get("bench_url") or ((result.get("job") or {}).get("result") or {}).get("bench_url")
            if bench:
                sys.stderr.write(f"Open the draft on the Bench: {bench}\n")
            elif result.get("mode") == "mixed":
                sys.stderr.write("When the job completes, its result's bench_url opens the draft on the Bench.\n")
        return result
    s.set_defaults(func=_build)

    s = sub.add_parser("ship", help="build, publish and deliver in one run, after one approval of the run's total; spends credits")
    s.add_argument("tree"); s.add_argument("--title", required=True); s.add_argument("--description", default="")
    s.add_argument("--deliver", default=None,
                   help="comma-separated: package, app:lightweight, app:enterprise, kit (default: $SDCMODELER_DELIVER, else package)")
    s.add_argument("--dir", default=None, help="where the package and application land (default: $SDCMODELER_OUTPUT_DIR, else here)")
    s.add_argument("--project", default=None, help="for kit: the project holding supabase/ (default: $SDCMODELER_PROJECT_DIR, else --dir)")
    s.add_argument("--plan", action="store_true", help="print the run's total and stop; nothing is spent")
    s.add_argument("--yes", action="store_true", help="approve without the prompt (for scripts); the run is still refused when it cannot be paid")
    s.set_defaults(func=_ship)

    s = sub.add_parser("translate", help="translate a published model or component into another language and publish it; spends credits")
    s.add_argument("ct_id", help="the published model or component")
    s.add_argument("--language", required=True, help="the target language code, e.g. pt-BR")
    s.add_argument("--project", default=None, help="the target project's ct_id (default: your default project)")
    s.add_argument("--review", action="store_true", help="drafts only, to review in SDCStudio; nothing is published")
    s.add_argument("--plan", action="store_true", help="print the estimate and stop; nothing is spent")
    s.add_argument("--yes", action="store_true", help="approve without the prompt (for scripts); still refused when it cannot be paid")
    s.set_defaults(func=_translate)

    s = sub.add_parser("job", help="one job by id (from build), or every job with --list")
    s.add_argument("job_ct_id", nargs="?")
    s.add_argument("--list", action="store_true")
    s.set_defaults(func=lambda c, a: c.jobs() if a.list or not a.job_ct_id else c.job_status(a.job_ct_id))

    s = sub.add_parser("package", help="generate a published model's package and download it")
    s.add_argument("ct_id"); s.add_argument("--dir", default=".")
    s.set_defaults(func=lambda c, a: {"generated": c.generate_package(a.ct_id), "path": str(c.download_package(a.ct_id, a.dir))})

    s = sub.add_parser("app", help="generate and download a published model's application (1,500 credits the first time)")
    s.add_argument("ct_id"); s.add_argument("--profile", choices=["lightweight", "enterprise"], default="lightweight"); s.add_argument("--dir", default=".")
    def _app(c, a):
        generated = c.generate_app(a.ct_id, a.profile)
        path, charged = c.download_app(a.ct_id, a.dir, a.profile)
        return {"generated": generated, "path": str(path), "charged": charged}
    s.set_defaults(func=_app)

    s = sub.add_parser("kit", help="write a published model's Supabase kit into a project (1,500 credits the first time, shared with app)")
    s.add_argument("ct_id"); s.add_argument("--project", default=".", help="the project holding supabase/ (default: here)")
    def _kit(c, a):
        kit = c.supabase_kit(a.ct_id)
        return {"models": kit["models"], "written": write_kit(a.project, kit), "charged": kit.get("charged")}
    s.set_defaults(func=_kit)

    s = sub.add_parser("wallet", help="the wallet balance in credits and this key's policy")
    s.set_defaults(func=lambda c, a: {"wallet": c.wallet(), "key": c.key_policy()})

    sub.add_parser("mcp", help="run the MCP stdio server (options after `mcp` belong to the server)")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if argv[:1] == ["mcp"]:
        # The registry launches `sdcmodeler mcp [server options]`; the server owns those options.
        from sdcmodeler.mcp_server import main as mcp_main
        mcp_main(argv[1:])
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    client = SDCStudioClient(base_url=args.url)
    try:
        result = args.func(client, args)
        _print(result)
        # ship and translate: 3 when the run is still building on the server (do not run it again), 2 when the model is
        # published but a deliverable failed (or translated drafts were left unpublished); a script should notice either.
        if isinstance(result, dict) and result.get("status") == "in_flight":
            return 3
        if isinstance(result, dict) and result.get("status") == "drafts_unpublished":
            return 2
        return 2 if isinstance(result, dict) and result.get("failed") else 0
    except (SDCStudioError, KitConflictError, runs.RunRefused) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

"""``sdcmodeler``: the client at a terminal. ``sdcmodeler mcp`` starts the MCP server."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sdcmodeler import __version__
from sdcmodeler.client import SDCStudioClient, SDCStudioError


def _print(data) -> None:
    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def _load_tree(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
    s.set_defaults(func=lambda c, a: c.search_components(a.query, type=a.type, project=a.project, page_size=a.page_size))

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
    s.set_defaults(func=lambda c, a: c.estimate(_load_tree(a.tree), a.title))

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
        return result
    s.set_defaults(func=_build)

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
        _print(args.func(client, args))
        return 0
    except SDCStudioError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

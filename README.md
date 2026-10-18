# sdcmodeler

<!-- mcp-name: io.github.Axius-SDC/sdcmodeler -->

**The modeler's client for SDCStudio.** Search the public catalog, estimate, assemble and publish an SDC4 data model, generate its package and an application. A Python client with httpx as its only dependency, a CLI, and `sdcmodeler mcp`, an MCP server for any agent.

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

## Why

An agent that builds a feature decides a data model at that moment. With sdcmodeler the data is modeled in SDC by default: the agent searches the catalog first, reuses published components wherever one fits, mints only what is new, publishes the model, and takes the package. The result is interoperable because it is composed from shared, published components, and verifiable because every record validates against a published schema.

The name follows the role pattern of the other Semantic Data Charter servers: sdcvalidator validates, sdcgovernance governs, sdcreader reads, sdcmodeler models. It also names what SDCStudio calls every user: a Modeler.

## Install

```bash
pip install sdcmodeler
```

The catalog and estimates need no account. Building needs an SDCStudio API key, created in Settings, API Keys, with the scopes and spending limit the holder should have:

```bash
export SDCSTUDIO_API_KEY=sdcs_...
```

## The three layers

### The client

```python
from sdcmodeler import SDCStudioClient

c = SDCStudioClient()                       # key from $SDCSTUDIO_API_KEY, URL from $SDCSTUDIO_URL
hits = c.search_components("blood pressure", type="xdquantity")
tree = {
    "label": "Vitals",
    "components": [
        {"ct_id": hits["results"][0]["ct_id"]},            # reuse: free
        {"label": "Pulse", "data_type": "XdCount"},        # mint: 100 credits
    ],
    "clusters": [],
}
print(c.estimate(tree, "Vitals"))           # reuse, mint, total_credits, estimate_id; no call that spends
result = c.assemble("Vitals", tree)         # 500 credits plus the mints
```

Every leaf existing: the server assembles, publishes and generates the package in one call (`result["mode"] == "reuse"`). Some leaves new: the pipeline runs and `result["data_source_ct_id"]` is polled with `c.job_status(...)`.

A 402 is `InsufficientCredits` with the numbers in credits. A 403 from the key's policy (no `spend` scope, or past its limit) is `KeyPolicyRefused`. The client never funds a wallet; a person does that at the wallet page.

### The CLI

```bash
sdcmodeler search "blood pressure" --type xdquantity
sdcmodeler models "patient"
sdcmodeler model <ct_id>
sdcmodeler artifact <ct_id> xsd --dir ./schemas
sdcmodeler estimate tree.json --title "Vitals"
sdcmodeler build tree.json --title "Vitals"
sdcmodeler app <ct_id> --profile lightweight --dir ./apps
sdcmodeler wallet
```

### The MCP server

```bash
sdcmodeler mcp
```

A JSON-RPC 2.0 stdio server, no SDK, the way sdcvalidator and sdcgovernance are built. A client configuration needs only:

```json
{"command": "uvx", "args": ["sdcmodeler", "mcp"], "env": {"SDCSTUDIO_API_KEY": "sdcs_..."}}
```

| Tool | Does | Spends |
|---|---|---|
| `search_components` | the public catalog by label, type and project | no |
| `list_models`, `get_model` | published models and their artifact routes | no |
| `estimate_model` | what a tree would reuse, mint and cost; returns `estimate_id` | no |
| `build_model` | assemble and publish; needs the `estimate_id` the person saw; refuses past the key's limit | yes |
| `job_status` | the pipeline for a mixed build | no |
| `generate_app` | generate and download the application; needs `confirm_charge`; 1,500 credits the first time, free after | yes |
| `wallet` | balance in credits and the key's policy | no |

**The policy lives in the server.** No spend without an estimate shown: `build_model` takes the `estimate_id` that `estimate_model` returned for the same title and tree in this process. Stop at the key's limit: a build whose estimate exceeds what the key may still spend is refused before the server is asked. Never pay: a 402 is reported with the wallet page's address and the tool stops. No tool takes a URL, a key or a payment method as an argument; the key is the environment the server starts with.

## The package or the application

`get_model` names the artifacts: the XSD 1.1 schema, the instance template, JSON, JSON-LD, SHACL, HTML, GraphQL. The package is a contract with no runtime attached; a feature inside an existing application implements on it, in any language, validating every record locally with [sdcvalidator](https://pypi.org/project/sdcvalidator/). The generated application (lightweight or enterprise profile) is a Django implementation on the same package, right when the feature is a new service. [CordovaOS](https://github.com/Axius-SDC/CordovaOS) is ten of them sharing data with zero integration code.

## What is not here

Validation (sdcvalidator's server), governance (sdcgovernance's), receipt verification (sdcreceipt's) and bringing existing data across (sdc-agents) are their own registered servers. sdcmodeler composes with them; it does not re-implement them.

## Release

Two steps: merge to `main`, then push the tag `v<version>` matching `pyproject.toml`. `release.yml` runs the tests, builds, publishes to PyPI by trusted publisher and to the MCP registry.

## License

Apache License 2.0. Copyright Axius SDC, Inc.

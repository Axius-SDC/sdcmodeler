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
        {"label": "Pulse", "data_type": "XdCount"},        # mint: 200 credits (draft and publication)
    ],
    "clusters": [],
}
estimate = c.estimate(tree, "Vitals")       # the server's estimate: reuse, mint, problems, total_credits, wallet, key, estimate_id
result = c.assemble("Vitals", tree, estimate_id=estimate["estimate_id"])   # 500 credits plus 200 per new component
```

Every leaf existing: the server assembles, publishes and generates the package in one call (`result["mode"] == "reuse"`; `published` and `generated` say so). Some leaves new: the pipeline runs and `result["job_ct_id"]` is polled with `c.job_status(...)`, or `c.wait_for_job(...)`, until `completed`, when the job has published and generated as asked and its result carries the model's links. `publish=False` leaves a draft for a person to review. The server refuses a build whose tree changed since the estimate.

A 402 is `InsufficientCredits` with the numbers in credits. A 403 from the key's policy (no `spend` scope, or past its limit) is `KeyPolicyRefused`. The client never funds a wallet; a person does that at the wallet page.

### The CLI

```bash
sdcmodeler search "blood pressure" --type xdquantity
sdcmodeler models "patient"
sdcmodeler model <ct_id>
sdcmodeler artifact <ct_id> xsd --dir ./schemas
sdcmodeler estimate tree.json --title "Vitals"
sdcmodeler build tree.json --title "Vitals" --wait
sdcmodeler job <job_ct_id>
sdcmodeler package <ct_id> --dir ./packages
sdcmodeler app <ct_id> --profile lightweight --dir ./apps
sdcmodeler kit <ct_id> --project ./my-app
sdcmodeler ship tree.json --title "Vitals" --deliver package,kit --project ./my-app
sdcmodeler translate <ct_id> --language pt-BR --plan
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
| `search_components` | published components by label, type and project: with a key your own first (private included, `scope: mine`), then the public catalog | no |
| `list_models`, `get_model` | published models and their artifact routes | no |
| `estimate_model` | what a tree would reuse, mint and cost; returns `estimate_id`; with `deliver`, the whole run's total and whether the wallet and the key can pay it | no |
| `build_model` | assemble, publish and generate (or `publish: false` for a draft); needs the `estimate_id` the person saw; refuses past the key's limit | yes |
| `job_status` | one job by id: status, step, cost so far, the model's links | no |
| `download_package` | a model's package ZIP with the key, including a model in a private project; builds a missing package first (free, once) unless `generate` is false | no |
| `generate_app` | generate and download the application; needs `confirm_charge`; 1,500 credits the first time, free after | yes |
| `add_supabase_kit` | write a model's Supabase kit into a project (`project_dir`); needs `confirm_charge`; charged with the application, once per model | yes |
| `build_and_deliver` | one run: build, publish, wait, then deliver the package, application or kit; needs the `estimate_id` returned with the same `deliver`; refuses before building when the total cannot be paid | yes |
| `estimate_translation` | what translating a published model or component into another language would reuse, copy and cost, and which enumeration values have no definition IRI; returns `estimate_id`; never spends, even with auto-spend | no |
| `translate_model` | translate and publish (or `review: true` for drafts); needs the `estimate_id` the person saw, or an auto-spend key | yes |
| `wallet` | balance in credits and the key's policy | no |

**The policy lives in the server.** No spend without an estimate shown: `build_model` takes the `estimate_id` that `estimate_model` returned for the same title and tree in this process. Stop at the key's limit: a build whose estimate exceeds what the key may still spend is refused before the server is asked. Never pay: a 402 is reported with the wallet page's address and the tool stops. One approval per run: `build_and_deliver` follows the run's total the person saw, which covers the application or kit charge, so it needs no `confirm_charge`. No tool takes a URL, a key or a payment method as an argument; the key is the environment the server starts with.

## The package or the application

`get_model` names the artifacts: the XSD 1.1 schema, the instance template, JSON, JSON-LD, SHACL, HTML, GraphQL. The package is a contract with no runtime attached; a feature inside an existing application implements on it, in any language, validating every record locally with [sdcvalidator](https://pypi.org/project/sdcvalidator/). The generated application (lightweight or enterprise profile) is a Django implementation on the same package, right when the feature is a new service. The Supabase kit is the same for a TypeScript app on Supabase: typed code for the model's records, migrations, and a gate (an Edge Function) that validates every record against the published model before storing it. `sdcmodeler kit` and `add_supabase_kit` write it into the project: the first model brings the project base, each further one adds its kit and one migration, numbered after the project's latest, and a component already in the project is never rewritten. Apply with Supabase's CLI (`supabase db push`, `supabase functions deploy gate`). [CordovaOS](https://github.com/Axius-SDC/CordovaOS) is ten of them sharing data with zero integration code.

## Walkthrough for an SDCStudio team

### Set up once

The account holder does this in SDCStudio at [sdcstudio.axius-sdc.com](https://sdcstudio.axius-sdc.com); each developer then installs the package and exports their key.

1. Fund the wallet. Building is paid in credits, 1,000 to the dollar. sdcmodeler never adds funds; a person does that at the wallet page.
2. Create one API key per developer or agent in Settings, API Keys. Without the `spend` scope a key can search, estimate, read models and download packages. With it, the key's spending limit caps what that holder may spend.
3. Check a key at any time with `sdcmodeler wallet`: the balance and that key's policy.

On an SDCStudio deployment with the build-and-deliver release, three more settings decide how far an agent goes without asking. A signed-in person makes all three; a key can spend within them and can never change them:

- **Auto-spend on the key.** The agent runs `build_and_deliver` without a person approving each estimate. A spending limit stays optional: with none, the agent spends in the wallet's reload amounts, under the monthly maximum below.
- **Auto-reload on the wallet, confirmed.** When a run needs more than the balance, SDCStudio tops the wallet up from the saved card, in the configured amount, before anything is published. A key may trigger a reload only after the person has confirmed auto-reload, and the person gets one email per reload a key triggers.
- **A monthly auto-reload maximum.** The most auto-reload adds in a calendar month. A wallet's first funding sets it to four times that amount, and sets the reload amount to the amount itself ($10 funded: $10 reloads, at most $40 a month).

| Key | Auto-reload | A run |
|---|---|---|
| no `spend` | any | searches, estimates, reads models, downloads packages; no charge |
| `spend` | off | the person approves each estimate; a short balance stops it before anything is published |
| `spend` | confirmed | the person approves each estimate; a short balance is topped up first |
| `spend` + auto-spend | off | no per-run approval; a short balance stops it before anything is published |
| `spend` + auto-spend | confirmed | no per-run approval; a short balance is topped up first, within the monthly maximum |

Organization wallets never auto-reload; the organization's admin sets each member's limit.

### With a coding agent

Add the MCP configuration above to the agent. A typical session, for a developer adding a vitals form to a clinic app:

1. The developer asks: "Add a form to record blood pressure and pulse."
2. The agent calls `search_components` and finds a published blood pressure component.
3. It drafts a tree that reuses that component and mints a new Pulse count, then calls `estimate_model`.
4. It shows the developer the estimate: one component reused, one minted, the total in credits.
5. The developer approves. The agent calls `build_model` with that `estimate_id`, follows `job_status` if a job runs, then calls `download_package` or `add_supabase_kit`.
6. The agent writes the feature against the published model.

The agent cannot skip step 4: `build_model` refuses without the `estimate_id` returned for the same title and tree in the same session.

### With the CLI

The one piece written by hand is the tree. A component with a `ct_id` is reused; one with a `label` and an SDC4 `data_type` (such as `XdString`, `XdCount`, `XdQuantity` or `XdTemporal`) is minted. Clusters group components and nest.

```json
{
  "label": "Vitals",
  "components": [
    {"ct_id": "<ct_id from search>"},
    {"label": "Pulse", "data_type": "XdCount"}
  ],
  "clusters": []
}
```

```bash
sdcmodeler search "blood pressure" --type xdquantity
sdcmodeler estimate tree.json --title "Vitals"
sdcmodeler build tree.json --title "Vitals" --estimate-id <id> --wait
sdcmodeler package <ct_id> --dir ./packages
```

Builds land in the Modeler's default project, created on first use if there is none.

### In one run

`ship` (CLI) and `build_and_deliver` (MCP) do the whole path after one approval: build, publish every new component and the model into the Modeler's private default project, wait for the job, then deliver.

```bash
sdcmodeler ship tree.json --title "Vitals" --deliver package,kit --project ./my-app --plan   # the total; nothing spent
sdcmodeler ship tree.json --title "Vitals" --deliver package,kit --project ./my-app          # asks once, then runs
```

The total covers the build and the deliverables: 1,500 credits once when they include an application or a kit, since one charge covers both. A run the wallet or the key cannot pay is refused before anything is built. Once the model is published, a deliverable that fails is reported under `failed`, the others still run, and `ship` exits 2; fetch the failed one with its own command. `--yes` approves without the prompt, for scripts.

On a deployment with the build-and-deliver release, SDCStudio does the run itself: it funds and holds the whole total before the first publish (a reload included, when the person allows one), delivers each item from the hold, and sdcmodeler downloads what is ready and writes the kit. On an older deployment sdcmodeler composes the run from the calls it already has. Which one is decided by what the server's estimate answers, never by a setting. With an auto-spend key, `build_and_deliver` needs no prior estimate and returns the estimate in its result; `ship` still asks at the terminal unless `--yes`.

The deliverables can live in the agent's settings instead of each call: `SDCMODELER_DELIVER` (for example `package,kit`), `SDCMODELER_OUTPUT_DIR` for the package and application, and `SDCMODELER_PROJECT_DIR` for the kit, in the environment of the CLI or of the MCP server's configuration.

### Translating a model

A published model or component can be translated into another language, and the translation published, in one call. SDCStudio copies every component the source needs into the target language, and each copy keeps its link to its original, its semantic links and its enumeration definition IRIs: a pt-BR record and an en-US record of the same data have their own structures and meet in the graph. The human text of the model's data tree is translated: labels, descriptions, enumeration values, boolean values, ordinal symbols, and the model's title and description. The model's contextual components (audit, attestation, access control, protocol, workflow, parties) and its Units stay in their original language, and links and constraints never change. The language is any BCP 47 language tag, such as `es-ES`, `sw` or `zh-Hant-TW` (SDCStudio 4.28.1; it is stored in canonical form, so `pt-br` is `pt-BR`); `SDCStudioClient.translate_languages(q)` searches the registry by name or tag. A translation that already exists is reused: a published one free, your own draft in the target project published in the run and charged for publication only.

```bash
sdcmodeler translate <ct_id> --language pt-BR --plan     # what it reuses, copies and costs; spends nothing
sdcmodeler translate <ct_id> --language pt-BR            # asks, then translates and publishes into your default project
sdcmodeler translate <ct_id> --language pt-BR --review   # drafts only, to review beside the originals in SDCStudio
```

With a coding agent, `estimate_translation` then `translate_model` with its `estimate_id`, or `translate_model` alone on an auto-spend key. The estimate lists **unanchored** enumeration values: values with no definition IRI, which once translated have nothing tying them to the original. Look at those before approving. The result is `completed` (with the translated model's id and links), `drafts_unpublished` (made and charged, but publishing failed: translate again to finish, charged for publication only, or publish the drafts in SDCStudio; `translate` exits 2), or `in_flight` (still running on the server: poll the job, never translate again; exits 3). Needs SDCStudio 4.28.0.

### What it costs

Reuse is free, so the cheapest model is the one built most from published components. The Vitals tree costs 700 credits ($0.70): 500 for the model, and 200 for Pulse (100 for its draft, 100 to publish it).

| Action | Credits | USD |
|---|---|---|
| Search, estimate, read models, download a package | 0 | $0.00 |
| Reuse a published component | 0 | $0.00 |
| A new draft of a component or a cluster below the root: minted, copied or translated | 100 each | $0.10 |
| Publish a component or a cluster below the root | 100 each | $0.10 |
| A model: its draft, its root cluster and its publication | 500 | $0.50 |
| Generate an application or Supabase kit, first time per user per model | 1,500 | $1.50 |

### Review before publishing

Publishing locks a model permanently; that is what lets every Governed Data Record keep validating against the schema it was made for. A team with a data steward builds drafts (`--draft`, `publish: false` in `build_model`, or `publish=False` in the client), priced without the publication charges (`estimate --draft`, `publish: false` in `estimate_model`), the steward reviews and publishes in SDCStudio, and the developer then takes the package, application or kit as usual.

### When a call is refused

| You see | It means | Do this |
|---|---|---|
| 402, `InsufficientCredits` | The wallet cannot cover the build; cost and balance are reported in credits. A run also says why (`reason`): `auto_reload_off`, `no_card`, `auto_reload_unconfirmed`, `ceiling`, `declined`, `reload_failed`, `organization_wallet`, `insufficient_funds`. Nothing was published or charged | Add funds at the wallet page, or fix what the reason names (turn on or confirm auto-reload, save or update the card, raise the monthly maximum, ask the organization admin), then run again |
| 403, `KeyPolicyRefused` | The key lacks a scope the call needs, or is past its spending limit | Ask the account holder to adjust the key |
| 409, stale estimate (`AssemblyRefused`) | The tree changed after the estimate | Estimate again and build with the new id |
| 409, retired component (`AssemblyRefused`) | The tree reuses a retired component | Search for its replacement and update the tree |
| 409, `TranslationRefused` | `stale_estimate` (the source, language, project or review changed), `retired` (with the `successor` to translate instead), or `same_language`. Nothing was created | Estimate again, translate the successor, or pick another language |
| Problems in the estimate | A component needs a `ct_id`, or a `label` and `data_type`; a cluster needs a label | Fix the tree; nothing was built |

## What is not here

Validation (sdcvalidator's server), governance (sdcgovernance's), receipt verification (sdcreceipt's) and bringing existing data across (sdc-agents) are their own registered servers. sdcmodeler composes with them; it does not re-implement them.

## Release

Two steps: merge to `main`, then push the tag `v<version>` matching `pyproject.toml`. `release.yml` runs the tests, builds, publishes to PyPI by trusted publisher and to the MCP registry.

## License

Apache License 2.0. Copyright Axius SDC, Inc.

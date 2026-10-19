# Changelog

All notable changes to `sdcmodeler` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions carry the leading 4
of the SDC4 reference model.

## [Unreleased]

## [4.3.0] - 2026-10-19

### Added

- **`download_package`**, an MCP tool: downloads a model's package with the
  key into the output directory, which works for a model in a private
  project, such as the default project `build_model` builds into. `get_model`
  and the public catalog cover public models only, so an agent could build a
  model over MCP and not fetch its package. When the model is published and
  its package not yet built, the tool builds it first (free, once; it needs
  the right to publish the model); `generate: false` only downloads. The
  server's instructions name it.

## [4.2.1] - 2026-10-19

### Fixed

- **The local estimate follows SDCStudio 4.17.5's one rule**: 500 for the
  model (its root cluster included), 100 for every other new cluster, 100 for
  every new component. A new nested cluster was not counted, so the local
  figure could be 100 short of the server's. `mode` is the path `assemble/`
  takes: a new component makes it `mixed`, a new cluster alone does not.
- **`estimate_id` hashes the trimmed title**, as the server does; a title with
  a leading or trailing space was refused by `assemble/` as a stale estimate.
- **The wallet address** the MCP server gives on a 402 is
  `/app/settings?tab=wallet`; `/app/wallet` does not exist.
- The README's CLI list includes `job` and `package`.

## [4.2.0] - 2026-10-18

### Added

- **`publish` and `generate` on `assemble`** (SDCStudio 4.17.2), both true by
  default: the reuse path answers with `published`, `generated` and
  `model_url`; the mixed path's job publishes bottom-up and builds the package
  when the pipeline completes, and its result says so. `publish=False` leaves
  a draft for a person to review. The MCP `build_model` takes both and tells
  the agent what to poll; the CLI's `build` has `--draft` and `--no-generate`.
- **`wait_for_job`** polls a job until it completes or fails, backing off;
  `build --wait` uses it on the mixed path.

## [4.1.0] - 2026-10-18

### Changed

- **The estimate is the server's when a key is set** (SDCStudio 4.17.0,
  `POST /api/v1/dmgen/assemble/estimate/`): every leaf resolved as `assemble/`
  resolves it, problems named (unknown, unpublished, retired with its
  successor), new clusters counted as mints, the wallet's and the key's
  position in the answer, and the same `estimate_id` the local computation
  produces. Without a key, or against an older deployment, the local estimate
  is used and marked `"source": "local"`.
- **`assemble` sends `estimate_id`** and the server refuses a build whose title
  or tree changed since the estimate (409 `stale_estimate`). The MCP
  `build_model` passes the id it checked; two gates, one id.
- **`job_status` is the job resource** (`GET /api/v1/jobs/<ct_id>/`): status,
  step, cost so far, the model's links on completion, the reason on failure.
  `assemble` on the mixed path returns `job_ct_id`; the MCP tool takes it. The
  pipeline's own status stays available as `pipeline_status`.

### Added

- `jobs()` lists the caller's jobs; `generate_package(..., as_job=True)` and
  `generate_app(..., as_job=True)` answer a job instead of waiting; the CLI's
  `job` takes an id or `--list`, and `build` takes `--estimate-id`.

## [4.0.0] - 2026-10-18

### Added

- **The client** (`sdcmodeler.client.SDCStudioClient`): the public catalog
  (components, models, one model with its artifact routes, artifact download
  following the storage pointer production answers, without the key), the
  Modeler and the default project (created on first use), the wallet, the
  key's own policy row, a local estimate, assembly on both paths, the pipeline
  status, publish, package generation and download, application generation
  and download (the server's once-per-model charge reported as `charged`).
  Errors as the server means them: `InsufficientCredits` (402, credits),
  `KeyPolicyRefused` (403, `key_scope` or `key_spend_limit`),
  `AssemblyRefused` (409, a retired component).
- **The estimate** (`sdcmodeler.pricing.estimate_tree`): the tree resolved the
  way the server resolves it, reuse free and mints 100 credits each on top of
  the model's 500, with an `estimate_id` over the canonical tree and title.
- **The CLI**: `search`, `models`, `model`, `artifact`, `estimate`, `build`,
  `job`, `package`, `app`, `wallet`, and `mcp`.
- **The MCP server** (`sdcmodeler mcp`): eight tools over the client with the
  product policy in the server: `build_model` needs the `estimate_id` the
  person saw and refuses past the key's limit; `generate_app` needs
  `confirm_charge`; a 402 is reported with the wallet page and the tool stops.
  Raw JSON-RPC 2.0 over stdio, no SDK; malformed input is answered, never
  fatal. `server.json` passes `mcp` so a registry client starts the server.

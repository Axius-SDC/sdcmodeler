# Changelog

All notable changes to `sdcmodeler` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions carry the leading 4
of the SDC4 reference model.

## [Unreleased]

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

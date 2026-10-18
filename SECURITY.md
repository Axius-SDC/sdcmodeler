# Security

Report a vulnerability privately to security@axius-sdc.com, or through GitHub's
private vulnerability reporting on this repository. Expect an acknowledgement
within three business days.

## Supported versions

| version | supported |
|---|---|
| 4.0.x | yes |

## The MCP server, as a threat model

`sdcmodeler mcp` speaks JSON-RPC over stdio. There is no authentication and no
network listener: whoever can write to the process's stdin is the caller, and
that caller controls every byte of every tool argument. The server is designed
for that:

- **The key never travels.** The SDCStudio API key is the environment the
  server starts with, never a tool argument, so it does not pass through the
  conversation or whatever records it. The storage host that serves artifacts
  never receives it.
- **No tool takes a URL** or a payment method. The server reaches one
  deployment, fixed at start-up.
- **Nothing is spent without an estimate shown.** `build_model` requires the
  `estimate_id` of an estimate made in the same process for the same tree;
  `generate_app` requires an explicit confirmation. A key's spending limit is
  checked before the server is asked, and enforced again by the server under
  its wallet lock.
- **Malformed input is answered, never fatal.** A request that is not an
  object, `params` that are not an object, a non-string method or tool name,
  or arguments that are not an object are answered with JSON-RPC errors and
  the loop keeps reading. A defect in the server is reported without a
  traceback.

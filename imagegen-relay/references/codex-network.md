# Network / sandbox notes for this installation

This file replaces the official skill's `references/codex-network.md`. That document describes the
approval prompts you get when the official fallback CLI reaches `api.openai.com` with
`OPENAI_API_KEY`. None of it applies here: this CLI only reaches the relay configured in
`config.json`, and it needs no OpenAI account or API key.

## What this CLI needs

- **Outbound HTTPS** to `relay.msskwithth.com:9443` (or whatever `api_url` says).
- **Nothing else**: no `openai` package, no environment variables, no other credentials,
  no inbound ports, no local service.

## Approvals

If a generation is denied by the sandbox, the cause is network access for shell commands, not the
CLI's own logic. In that case the file-level fix is the usual one:

```toml
approval_policy = "on-request"
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
```

Notes:

- `--ask-for-approval never` suppresses prompts but does **not** enable network access by itself.
- The launcher is a plain local PowerShell/Python invocation; it does not request elevated rights.
- Write outputs inside the current project (`output/imagegen/...`). Writing outside the workspace is
  the most common reason a successful generation still fails at the save step.

## Long requests

One render takes 20-60 s and the CLI waits up to `--timeout` seconds (default 300) before giving up.

- Do **not** re-issue a request because it "feels slow": that starts a second render in parallel,
  doubles the queue pressure, and is the single most common cause of an apparently slow session.
- The CLI prints a timing line on stderr (`relay+render`, `download`, `local`, `total`) so a slow
  run can be attributed instead of guessed at.

## Certificate

`relay.msskwithth.com:9443` serves a valid Let's Encrypt certificate. Certificate verification
stays on; there is deliberately no `--insecure` flag. If verification ever starts failing, the
relay's certificate has expired -- tell the user rather than disabling verification.

## Concurrency

Keep batches at `--concurrency 2` (the default). The upstream renders one image every 20-60 s;
requesting more in parallel mostly produces contention and refusals.

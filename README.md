# Ninja

Ninja is a browser-automation agent with a persistent Chromium session, Slack
messaging, a GitHub Issues work queue, and an orchestrator that completes one
queued issue per cycle. Operational details live in [`agent-docs/`](agent-docs/),
and reusable command-line helpers are documented in [`tools/README.md`](tools/README.md).

## Local setup

The managed Ninja sandbox is the supported runtime and already includes service
configuration and credentials. For local development, start from the repository
root and use Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install requests pydantic posthog playwright slackify-markdown pytest
playwright install chromium
```

Some integrations require credentials supplied by the managed environment:

- Slack needs a bot token in `SLACK_BOT_TOKEN` or `/dev/shm/mcp-token` plus a
  configured channel and agent (see [`agent-docs/SLACK_INTERFACE.md`](agent-docs/SLACK_INTERFACE.md)).
- GitHub issue commands need an authenticated `gh` CLI session or token.
- Browser automation expects the persistent Chromium/CDP service; see
  [`agent-docs/NINJA_SPEC.md`](agent-docs/NINJA_SPEC.md).

## Verify the installation

Run the deterministic checks first:

```bash
python -c "import requests, pydantic, playwright; print('Python dependencies: OK')"
pytest -q
python tools/issues.py count --json
python messaging/slack/interface.py config
```

In a managed installation, confirm the worker and browser endpoints too:

```bash
systemctl status ninja.service
curl -fsS http://127.0.0.1:9222/json/version
```

The setup is healthy when the test suite passes, the issue command returns JSON,
and the configured integrations report valid connectivity. A stopped
`ninja.service` is normal between one-shot work cycles; an active cycle can be
started with `systemctl start ninja.service`.

## Troubleshooting

### Missing Python package

Activate the virtual environment and reinstall the dependency named in the
traceback. For browser import errors, run both `python -m pip install playwright`
and `playwright install chromium`.

### Slack is not connected

Provide a bot token, then configure the defaults:

```bash
python messaging/slack/interface.py config --set-channel "#your-channel"
python messaging/slack/interface.py config --set-agent ninja
python messaging/slack/interface.py read -l 1
```

Use `python messaging/slack/interface.py scopes` if an operation reports
`missing_scope`.

### Browser/CDP connection fails

Check whether Chromium is listening on port 9222 and query its CDP endpoint:

```bash
ss -ltnp | grep ':9222'
curl -fsS http://127.0.0.1:9222/json/version
```

Do not launch a second browser over an occupied CDP port. In the managed
environment, connect to the persistent browser rather than starting a new one.

### Port already in use

Find the owning process with `ss -ltnp` (or `lsof -i :PORT`) and either stop the
stale process or choose another port. Local web apps should bind to `0.0.0.0` in
dedicated sandboxes and must avoid reserved ports `22`, `5000`, `6080`, `8080`,
`9000`, `9020`, and `9222`; see
[`agent-docs/DEPLOYMENT_RULES.md`](agent-docs/DEPLOYMENT_RULES.md).

### Service fails or exits immediately

```bash
systemctl status ninja.service --no-pager
journalctl -u ninja.service -n 100 --no-pager
python tools/health_check.py --json
```

The first traceback or failed health-check component is usually the actionable
cause; inspect components individually because optional checks may not apply to
every local checkout.

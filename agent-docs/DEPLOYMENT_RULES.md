## Deployment Rules

Check your sandbox type: `python3 -c "import json; print(json.load(open('/dev/shm/sandbox_metadata.json')).get('sandbox_provider', 'shared'))"`

If `sandbox_provider` is `ninja-dedicated`, you are on a dedicated sandbox where users can access deployed services directly. When deploying or running any server/service:

1. Bind to **`0.0.0.0`** (all interfaces), **not** `localhost` or `127.0.0.1`.
   Example: `python3 -m http.server 8899 --bind 0.0.0.0`, or `--host 0.0.0.0` for frameworks.
2. Run it in the background so it stays up (e.g. `nohup … &` or a supervisor entry).
3. Avoid ports already in use: 22, 5000, 6080, 8080, 9000, 9020, 9222.

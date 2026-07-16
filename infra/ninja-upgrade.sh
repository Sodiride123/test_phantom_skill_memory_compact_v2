#!/bin/bash
# ninja-upgrade.sh — Recurring in-sandbox upgrade job.
#
# Polls the published distribution for a newer version and, if found, applies
# dev's changes to the customer's repo as a 3-way `git merge` that preserves the
# customer's own instance edits. The LLM is invoked only to resolve genuine
# merge conflicts. A smoke check gates the result; failure rolls back to the
# pre-upgrade tag.
#
# Runs as a systemd oneshot fired by ninja-upgrade.timer. Safe to run by hand.
#
# Local testing: every side effect (package source, smoke check, notify, LLM
# resolve, push) is overridable via env hooks so the flow can run against a
# throwaway repo with no AWS/systemd. See poc/run.sh.
#
# Env hooks (all optional; prod defaults shown):
#   NINJA_HOME            /workspace/ninja        repo the job mutates
#   NINJA_BASELINE_BRANCH ninja-upstream          pristine upstream baseline
#   NINJA_PACKAGE_URL     (resolved from metadata) zip source (supports file://)
#   MESSAGING_CHANNEL     slack                    used to resolve the URL
#   NINJA_UPGRADE_PUSH    1                        push results to origin
#   NINJA_SMOKE_CMD       (built-in smoke_check)   override health verdict
#   NINJA_NOTIFY_CMD      (journal echo)           override notification sink
#   NINJA_LLM_RESOLVE_CMD (claude-wrapper.sh)      override conflict resolver
#   NINJA_FF_OVERRIDE     (query PostHog)          feature-flag override: 1=on, 0=off
#   NINJA_LOCKFILE        $NINJA_HOME/.ninja-git.lock

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
NINJA_HOME="${NINJA_HOME:-/workspace/ninja}"
BASELINE="${NINJA_BASELINE_BRANCH:-ninja-upstream}"
DO_PUSH="${NINJA_UPGRADE_PUSH:-1}"
HEALTH_CHECK="${NINJA_HEALTH_CHECK:-1}"     # 0 disables the differential health gate
UPGRADE_FLAG="${NINJA_UPGRADE_FLAG:-ninja-auto-upgrade}"   # PostHog feature-flag key
LOCKFILE="${NINJA_LOCKFILE:-$NINJA_HOME/.ninja-git.lock}"
GIT_ID=(-c user.name=ninja -c user.email=ninja@ninjatech.ai)
PRE_HEALTH=""                              # pre-upgrade health snapshot (set in main)
BASELINE_PREV=""                           # ninja-upstream tip before update_baseline (for rollback)
PACKAGE_URL=""                             # resolved by resolve_package_url (set in the current shell)

STAGING_DIR=""
WORKTREE_DIR=""
cleanup() {
    [[ -n "$WORKTREE_DIR" && -d "$WORKTREE_DIR" ]] && \
        git -C "$NINJA_HOME" worktree remove --force "$WORKTREE_DIR" 2>/dev/null || true
    [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" ]] && rm -rf "$STAGING_DIR"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Logging
#
# All diagnostic output goes to STDERR so it lands in the systemd journal
# (SyslogIdentifier=ninja-upgrade) and never corrupts values that helper
# functions echo to STDOUT for capture (e.g. resolve_package_url).
# Level-filtered via NINJA_UPGRADE_LOG_LEVEL (DEBUG|INFO|WARN|ERROR).
# ---------------------------------------------------------------------------
LOG_LEVEL="${NINJA_UPGRADE_LOG_LEVEL:-INFO}"

_lvl_num() {
    case "$1" in
        DEBUG) echo 10 ;; INFO) echo 20 ;; WARN) echo 30 ;; ERROR) echo 40 ;; *) echo 20 ;;
    esac
}

_log() { # _log <LEVEL> <message...>
    local level="$1"; shift
    [[ "$(_lvl_num "$level")" -lt "$(_lvl_num "$LOG_LEVEL")" ]] && return 0
    printf 'ninja-upgrade %-5s %s\n' "$level" "$*" >&2
}

log()       { _log INFO  "$@"; }   # default level; existing call sites keep working
log_warn()  { _log WARN  "$@"; }
log_error() { _log ERROR "$@"; }
log_debug() { _log DEBUG "$@"; }

# emit_result <outcome> — machine-readable final marker (stdout) the dashboard
# classifier reads: upgraded | rolled_back | conflict | error | up_to_date |
# disabled. Keeps the UI/telemetry decoupled from log prose.
emit_result() { echo "NINJA_UPGRADE_RESULT=$1"; }

# posthog_capture(status, message) — best-effort PostHog event for an upgrade
# outcome. Emits "ninja upgrade" with error=0 for success, 1 for failure
# (mirrors health_service's error flag), plus status + version. No-ops silently
# when PostHog is unconfigured, and never affects the run's exit path.
posthog_capture() {
    local err=1; [[ "$1" == success ]] && err=0
    ( cd "$NINJA_HOME" && PYTHONPATH="/workspace:$NINJA_HOME" \
        /usr/local/bin/python -c \
        "import sys; from clients.posthog_client import capture; capture('ninja upgrade', {'error': int(sys.argv[3]), 'status': sys.argv[1], 'version': sys.argv[2], 'message': sys.argv[4]})" \
        "$1" "${NEW_VERSION:-unknown}" "$err" "$2" ) >/dev/null 2>&1 || true
}

# notify(level, message) — outcome signal: success | conflict | rollback | error.
# Channel delivery (Slack/Teams/WhatsApp); for now this is logging-only, plus
# the NINJA_NOTIFY_CMD test hook used by the local harness.
# Success and failure outcomes emit a PostHog "ninja upgrade" event for fleet monitoring.
notify() {
    local level="$1"; shift
    if [[ -n "${NINJA_NOTIFY_CMD:-}" ]]; then
        "$NINJA_NOTIFY_CMD" "$level" "$*" || true
    fi
    _log INFO "notify[$level] $*"
    case "$level" in
        success|error|rollback) posthog_capture "$level" "$*" ;;
    esac
}

# ---------------------------------------------------------------------------
# Package resolution / download  (mirrors ninja-install.sh)
# ---------------------------------------------------------------------------
# Sets the global PACKAGE_URL. Call in the current shell (not a subshell/$( ))
resolve_package_url() {
    # Full-URL escape hatch (simplest for one-off tests).
    if [[ -n "${NINJA_PACKAGE_URL:-}" ]]; then PACKAGE_URL="$NINJA_PACKAGE_URL"; return; fi

    local channel base
    channel="${MESSAGING_CHANNEL:-slack}"
    if [[ -n "${NINJA_CDN_BASE:-}" ]]; then
        # Local/e2e: identical path shape to prod, only the host/scheme differs
        # (e.g. http://localhost:8000 or file:///tmp/cdn). Exercises the real
        # channel + latest.zip path construction instead of bypassing it.
        base="$NINJA_CDN_BASE"
    else
        local environment
        environment=$(jq -r '.environment' /dev/shm/sandbox_metadata.json)
        case $environment in
            beta|gamma) base="https://apps.super.${environment}myninja.ai" ;;
            prod)       base="https://apps.super.myninja.ai" ;;
            *)          notify error "unknown environment '$environment' — cannot resolve package URL"
                        emit_result error; exit 1 ;;
        esac
    fi
    PACKAGE_URL="${base}/_dist/ninja/${channel}/phantom-latest.zip"
}

# Download + unzip into STAGING_DIR; sets NEW_VERSION. Aborts on a corrupt zip.
download_staging() {
    local url="$1"
    STAGING_DIR=$(mktemp -d /tmp/ninja-upgrade.XXXXXX)
    log "Downloading package: $url"
    if ! curl -fsSL -o "$STAGING_DIR/pkg.zip" "$url"; then
        notify error "download failed: $url"; emit_result error; exit 1
    fi
    if ! unzip -tq "$STAGING_DIR/pkg.zip" >/dev/null 2>&1; then
        notify error "corrupt zip — aborting, will retry next cycle"; emit_result error; exit 1
    fi
    unzip -q -d "$STAGING_DIR" "$STAGING_DIR/pkg.zip"
    if [[ ! -f "$STAGING_DIR/ninja/VERSION" ]]; then
        notify error "zip missing ninja/VERSION — aborting"; emit_result error; exit 1
    fi
    NEW_VERSION=$(tr -d '[:space:]' < "$STAGING_DIR/ninja/VERSION")
}

# ---------------------------------------------------------------------------
# Baseline branch management
# ---------------------------------------------------------------------------
baseline_version() {
    git -C "$NINJA_HOME" show "$BASELINE:VERSION" 2>/dev/null | tr -d '[:space:]' || echo ""
}

# Ensure a local ninja-upstream exists and is a true ancestor of customer-main.
# Order: local branch → origin branch → root it at the install commit (never
# orphan-create; that yields no merge base and conflicts every file).
ensure_baseline() {
    git -C "$NINJA_HOME" fetch origin "$BASELINE" 2>/dev/null || true
    if git -C "$NINJA_HOME" show-ref --verify --quiet "refs/heads/$BASELINE"; then
        return
    fi
    if git -C "$NINJA_HOME" show-ref --verify --quiet "refs/remotes/origin/$BASELINE"; then
        git -C "$NINJA_HOME" branch "$BASELINE" "origin/$BASELINE"
        return
    fi
    log "No $BASELINE branch — bootstrapping from the install commit"
    local root
    root=$(git -C "$NINJA_HOME" log --grep='^Initialize ninja' --format=%H | tail -1)
    [[ -z "$root" ]] && root=$(git -C "$NINJA_HOME" rev-list --max-parents=0 HEAD | tail -1)
    git -C "$NINJA_HOME" branch "$BASELINE" "$root"
    log_warn "Bootstrapped $BASELINE at $(git -C "$NINJA_HOME" rev-parse --short "$root") — first merge may be heavier"
}

# update_baseline — advance ninja-upstream to the new zip content.
#
# Guarantees:
#   • ninja-upstream HEAD = exactly the zip's content, minus gitignored files
#     (rsync mirrors the tree; `git add -A` honours .gitignore so runtime files
#     never land on the baseline — Phase 1).
#   • upstream deletions/renames carry through (rsync --delete).
#   • linear history: a single commit is appended to the branch tip, no merge.
#
# Deviation from "git checkout ninja-upstream; rsync into
# /workspace/ninja" step: we stage into an isolated `git worktree` instead, so
# the live working tree — and the customer's gitignored instance files
# (settings.json, memory/, logs/ …) — are never touched by `rsync --delete`.
# Checking the branch out in place would delete those from disk mid-upgrade.
update_baseline() {
    WORKTREE_DIR=$(mktemp -d /tmp/ninja-baseline.XXXXXX)
    git -C "$NINJA_HOME" worktree add -f "$WORKTREE_DIR" "$BASELINE" >/dev/null
    # --checksum: compare by content, not size+mtime. Same-length edits
    # (e.g. a version string 0.1.0 → 0.1.1) are otherwise skipped by rsync's
    # default quick-check, silently producing a no-op "upgrade".
    rsync -a --checksum --delete --exclude='.git' "$STAGING_DIR/ninja/" "$WORKTREE_DIR/"
    git -C "$WORKTREE_DIR" add -A
    if git -C "$WORKTREE_DIR" diff --cached --quiet; then
        log "Baseline already at v$NEW_VERSION content — nothing to commit"
    else
        git -C "$WORKTREE_DIR" "${GIT_ID[@]}" commit -q -m "ninja v$NEW_VERSION"
    fi
    git -C "$NINJA_HOME" worktree remove --force "$WORKTREE_DIR"; WORKTREE_DIR=""
}

# ---------------------------------------------------------------------------
# Conflict resolution — one bounded LLM attempt, then give up.
# ---------------------------------------------------------------------------
LLM_TIMEOUT="${NINJA_LLM_TIMEOUT:-300}"   # seconds; conflicts shouldn't take long

# Returns 0 if the merge is fully resolved (no unmerged paths / markers), else 1.
resolve_conflicts() {
    local conflicted
    conflicted=$(git -C "$NINJA_HOME" diff --name-only --diff-filter=U)
    [[ -z "$conflicted" ]] && return 0

    notify conflict "LLM resolving: $(echo "$conflicted" | tr '\n' ' ')"

    if [[ -n "${NINJA_LLM_RESOLVE_CMD:-}" ]]; then
        # Test hook: <cmd> <repo> <file...>
        "$NINJA_LLM_RESOLVE_CMD" "$NINJA_HOME" "$conflicted" || true
    else
        # One bounded attempt via the standard wrapper. HEAD/"ours" is the
        # customer's edit; the incoming/"theirs" side is dev's published change.
        local prompt
        prompt="You are resolving git merge conflicts during an automated upgrade.
Repo: $NINJA_HOME (branch $(git -C "$NINJA_HOME" branch --show-current)).
For each file below, the conflict markers delimit two sides:
  <<<<<<< HEAD          = the CUSTOMER's own edit (preserve their intent)
  =======
  >>>>>>> $BASELINE     = DEV's published change for v$NEW_VERSION (apply the fix)
Merge both intents where possible; keep the customer's value only where it does
not defeat dev's fix. Remove ALL conflict markers. Edit files in place; do not
touch anything else. Conflicted files:
$conflicted"
        ( cd "$NINJA_HOME" && timeout "$LLM_TIMEOUT" ./claude-wrapper.sh -c -p "$prompt" ) || true
    fi

    # Stage whatever the resolver touched, then verify nothing is left unmerged.
    git -C "$NINJA_HOME" add -A 2>/dev/null || true
    [[ -n "$(git -C "$NINJA_HOME" diff --name-only --diff-filter=U)" ]] && return 1
    git -C "$NINJA_HOME" grep -lE '^(<<<<<<<|=======|>>>>>>>)' -- . >/dev/null 2>&1 && return 1
    return 0
}

# ---------------------------------------------------------------------------
# Smoke check — 0 = healthy (keep & push), non-zero = roll back.
# Default overridable via NINJA_SMOKE_CMD for testing. Assertions, short-circuit:
#   1. compiles + orchestrator imports
#   2. all core services (incl. ninja-monitor) active after restart
#   2b. still active after a soak window (catches slow crashes / restart loops)
#   3. dashboard + integrations endpoints answer
#   4. differential health: roll back only on a check that regressed ok→fail
# (Heartbeat-freshness was considered but rejected: POLL_INTERVAL=60s makes it
#  racy within a smoke window → false rollbacks; monitor staleness is covered by
#  the differential health gate's 5-min check instead.)
# ---------------------------------------------------------------------------
NINJA_SERVICES="ninja.service ninja-monitor.service ninja-dashboard.service ninja-integrations.service ninja-health.service"
SOAK_SECS="${NINJA_SMOKE_SOAK_SECS:-25}"   # extra settle to catch slow crashes / restart loops
STARTUP_TIMEOUT="${NINJA_SMOKE_STARTUP_SECS:-60}"   # max wait for services to come active
STARTUP_POLL="${NINJA_SMOKE_POLL_SECS:-2}"          # poll interval while waiting

restart_services() { systemctl restart $NINJA_SERVICES 2>/dev/null || true; }

# All core services must be active. ninja-monitor is the message-polling loop
# that actually drives the agent — a dead monitor means a broken upgrade even
# if the web tier answers, so it is gated here alongside the rest.
assert_services_active() {
    local svc
    for svc in $NINJA_SERVICES; do
        systemctl is-active --quiet "$svc" || { log_error "service not active: $svc"; return 1; }
    done
    if [[ "${MESSAGING_CHANNEL:-}" == "whatsapp" ]]; then
        systemctl is-active --quiet ninja-whatsapp-gateway.service \
            || { log_error "service not active: ninja-whatsapp-gateway"; return 1; }
    fi
    return 0
}

# Poll until all services are active, up to <timeout> seconds — returns as soon
# as they're up (no wasted wait) and only fails after the timeout (no false
# negative from a fixed sleep on a slow box). Per-poll checks are silenced; the
# final check logs which service is down.
wait_services_active() {  # <timeout-secs>
    local deadline=$(( SECONDS + ${1:-$STARTUP_TIMEOUT} ))
    while (( SECONDS < deadline )); do
        assert_services_active 2>/dev/null && return 0
        sleep "$STARTUP_POLL"
    done
    assert_services_active
}

# Write a health snapshot {check: 0|1} to $1 (best-effort). Returns 0 if written.
# health_service --once exits non-zero when checks fail; we want the JSON, not
# the code, so the failure is swallowed.
health_snapshot() {
    local out="$1"
    ( cd "$NINJA_HOME" && PYTHONPATH="/workspace:$NINJA_HOME" \
        /usr/local/bin/python processes/health_service.py --once --status-file "$out" ) \
        >/dev/null 2>&1 || true
    [[ -s "$out" ]]
}

# Differential gate: print the comma-separated checks that went ok(0)→fail(1)
# from <pre> to <post> and return 0 (regression). Return 1 if none regressed.
# Checks failing in BOTH snapshots (pre-existing/transient) never trigger.
health_regressed() {
    /usr/local/bin/python - "$1" "$2" <<'PY'
import json, sys
pre = json.load(open(sys.argv[1])); post = json.load(open(sys.argv[2]))
reg = sorted(k for k, v in post.items() if v and pre.get(k, 1) == 0)
if reg:
    print(",".join(reg)); sys.exit(0)   # regression → caller rolls back
sys.exit(1)                              # no regression
PY
}

# healthy (keep & push), non-zero = roll back.
smoke_check() {
    if [[ -n "${NINJA_SMOKE_CMD:-}" ]]; then
        "$NINJA_SMOKE_CMD"; return $?
    fi
    # 1. Compiles + orchestrator imports (same cwd/PYTHONPATH as ninja.service).
    ( cd "$NINJA_HOME" && PYTHONPATH="/workspace:$NINJA_HOME" \
        /usr/local/bin/python -c "import processes.orchestrator" ) || return 1
    # 2. Services restart and come active within the startup window (polled).
    restart_services
    wait_services_active "$STARTUP_TIMEOUT" || return 1
    # 2b. Soak: re-check after a further settle. A service that crashes a few
    #     seconds in (lazy import, crash-on-first-tick → Restart=on-failure loop)
    #     is "active" at 10s but "activating/failed" by now.
    sleep "$SOAK_SECS"
    assert_services_active || { log_error "a service crashed during the ${SOAK_SECS}s soak"; return 1; }
    # 3. Dashboard (9000) + integrations (9020) endpoints answer (bounded).
    curl -fsS --max-time 10 -o /dev/null "http://127.0.0.1:9000/" || return 1
    curl -fsS --max-time 10 -o /dev/null "http://127.0.0.1:9020/" || return 1
    # 4. Differential health: roll back only if a dependency the OLD code passed
    #    is now broken by the upgrade (creds/gateway/messaging/VPN regressions).
    if [[ "$HEALTH_CHECK" == "1" && -s "$PRE_HEALTH" ]]; then
        local post="$STAGING_DIR/health-post.json" reg
        if health_snapshot "$post"; then
            if reg=$(health_regressed "$PRE_HEALTH" "$post"); then
                log_error "health regressed after upgrade: $reg"
                return 1
            fi
            log "health checks: no regression vs pre-upgrade"
        else
            log_warn "post-upgrade health snapshot unavailable — skipping health gate"
        fi
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Feature-flag gate (per-user rollout via PostHog)
# ---------------------------------------------------------------------------
# ff_enabled <flag-key> — reusable: is a PostHog flag on for this user? (0/1)
# Any feature reuses this; no per-flag boilerplate. NINJA_FF_OVERRIDE is a
# generic test/manual override (1=on, 0=off, unset=query PostHog) so the local
# harness — or a manual run — works without PostHog wiring.
ff_enabled() {
    case "${NINJA_FF_OVERRIDE:-}" in 1) return 0 ;; 0) return 1 ;; esac
    ( cd "$NINJA_HOME" && PYTHONPATH="/workspace:$NINJA_HOME" \
        /usr/local/bin/python -c \
        "import sys; from clients.posthog_client import is_feature_enabled; sys.exit(0 if is_feature_enabled('$1') else 1)" \
    ) >/dev/null 2>&1
}

# Rollout / kill-switch for the upgrade: gated by the PostHog flag ($UPGRADE_FLAG)
# per user_id. Fails safe (off) if PostHog can't be reached.
upgrades_enabled() {
    ff_enabled "$UPGRADE_FLAG"
}

# ---------------------------------------------------------------------------
# Merge + finalize
# ---------------------------------------------------------------------------
# merge_upstream — tag the rollback point and 3-way merge the
# baseline into customer-main. Conflict policy is CUSTOMER-WINS: keeping the
# customer's edits intact is the primary goal, so on a genuine conflict (dev
# and customer changed the same line) the customer's line is kept and dev's
# change to that line is held back + reported. Dev's non-conflicting changes
# (including to the same file) are always applied.
#
# The LLM resolver is an explicit opt-in only (set NINJA_LLM_RESOLVE_CMD); it is
# NOT the default, because it can non-deterministically overwrite customer edits.
merge_upstream() {
    git tag -f "pre-upgrade-v$NEW_VERSION" >/dev/null
    log "Merging $BASELINE (v$NEW_VERSION) into $MAIN_BRANCH"
    if git "${GIT_ID[@]}" merge -m "Merge ninja v$NEW_VERSION into $MAIN_BRANCH" "$BASELINE"; then
        return 0   # dev changes applied, customer edits untouched
    fi

    local conflicted
    conflicted=$(git diff --name-only --diff-filter=U | tr '\n' ' ')

    # --- opt-in: experimental LLM resolution -------------------------------
    if [[ -n "${NINJA_LLM_RESOLVE_CMD:-}" ]]; then
        if resolve_conflicts; then
            git "${GIT_ID[@]}" commit -m "Merge ninja v$NEW_VERSION into $MAIN_BRANCH (conflicts resolved by LLM)"
            log "Conflicts resolved by LLM"
            return 0
        fi
        git merge --abort
        notify error "upgrade to v$NEW_VERSION needs a human — LLM could not resolve: ${conflicted}. $MAIN_BRANCH untouched."
        emit_result conflict
        exit 1
    fi

    # --- default: customer-wins --------------------------------------------
    # Redo the merge with -X ours so conflicting hunks keep the customer's
    # version while dev's non-conflicting changes still land.
    git merge --abort
    if git "${GIT_ID[@]}" merge -X ours \
        -m "Merge ninja v$NEW_VERSION into $MAIN_BRANCH (customer edits kept on conflicts)" "$BASELINE"; then
        log_warn "customer-wins: kept customer edits over dev changes on conflicting lines in: $conflicted"
        notify conflict "kept your edits on conflicting lines in: ${conflicted}— dev's changes there were held back for review"
        return 0
    fi

    # -X ours can't auto-resolve tree-level conflicts (e.g. modify/delete).
    local unresolved; unresolved=$(git diff --name-only --diff-filter=U | tr '\n' ' ')
    git merge --abort
    notify error "upgrade to v$NEW_VERSION needs a human — customer-wins couldn't auto-resolve: ${unresolved}. $MAIN_BRANCH untouched."
    emit_result conflict
    exit 1
}

# finalize — smoke-gate the merge. Healthy → push both branches
# (--force-with-lease, refuses if the remote moved) + notify. Unhealthy →
# reset customer-main to the pre-upgrade tag, restart on old code, notify.
finalize() {
    if smoke_check; then
        if [[ "$DO_PUSH" == "1" ]]; then
            # Push baseline first (append-only, least risky), then customer-main.
            # A push can fail (--force-with-lease refused if the remote moved, or
            # network/auth) — track it as an explicit error instead of letting
            # set -e abort silently. The merge is already applied locally, so a
            # later run retries the push.
            if ! git push origin "$BASELINE" --force-with-lease; then
                notify error "v$NEW_VERSION applied locally but pushing $BASELINE failed — will retry next run"
                emit_result error
                exit 1
            fi
            if ! git push origin "$MAIN_BRANCH" --force-with-lease; then
                notify error "v$NEW_VERSION applied locally but pushing $MAIN_BRANCH failed — will retry next run"
                emit_result error
                exit 1
            fi
        fi
        notify success "upgraded to v$NEW_VERSION"
        log "✓ Upgraded to v$NEW_VERSION"
        emit_result upgraded
    else
        log_error "✗ Smoke check failed — rolling back"
        git reset --hard "pre-upgrade-v$NEW_VERSION"
        # Rewind the baseline too, so the poll (which keys off ninja-upstream's
        # VERSION) sees the upgrade again next tick. Otherwise a transient smoke
        # failure would strand the box on the old code with baseline == NEW,
        # reporting "up to date" forever.
        [[ -n "$BASELINE_PREV" ]] && git branch -f "$BASELINE" "$BASELINE_PREV"
        restart_services
        notify rollback "v$NEW_VERSION failed smoke check — rolled back to pre-upgrade state"
        emit_result rolled_back
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    cd "$NINJA_HOME"
    [[ -d .git ]] || { log_error "no repo at $NINJA_HOME"; exit 0; }

    # Non-blocking lock shared with ninja-sync — a second tick just exits.
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCKFILE"
        flock -n 9 || { log_warn "another git op holds the lock — exiting"; exit 0; }
    else
        log_warn "flock unavailable — skipping lock (local mode only)"
    fi

    # Reuse the installed askpass if present (same as ninja-sync.service).
    if [[ -f /usr/local/bin/git-askpass.sh ]]; then
        export GIT_ASKPASS=/usr/local/bin/git-askpass.sh
    fi

    # --- Feature-flag gate (per-user rollout / kill-switch) ----------------
    if ! upgrades_enabled; then
        log "upgrades not enabled for this user (feature flag '$UPGRADE_FLAG' off) — exiting"
        emit_result disabled
        exit 0
    fi

    MAIN_BRANCH=$(git branch --show-current)   # global: used by merge_upstream/finalize
    # Detached HEAD → empty; merging/pushing an empty branch name is unsafe, so
    # bail before touching anything.
    if [[ -z "$MAIN_BRANCH" ]]; then
        notify error "detached HEAD in $NINJA_HOME (no current branch) — cannot upgrade safely, aborting"
        emit_result error
        exit 1
    fi

    # --- Poll ---------------------------------------------------------------
    resolve_package_url             # sets PACKAGE_URL
    download_staging "$PACKAGE_URL"
    ensure_baseline
    local CUR_VERSION; CUR_VERSION=$(baseline_version)

    if [[ "$NEW_VERSION" == "$CUR_VERSION" ]]; then
        log "Up to date (v$CUR_VERSION) — no-op"; emit_result up_to_date; exit 0
    fi
    if [[ -n "$CUR_VERSION" ]] && \
       [[ "$(printf '%s\n%s\n' "$CUR_VERSION" "$NEW_VERSION" | sort -V | tail -1)" == "$CUR_VERSION" ]]; then
        log "Published v$NEW_VERSION is not newer than baseline v$CUR_VERSION — skipping"; exit 0
    fi
    log "Upgrade available: v${CUR_VERSION:-none} → v$NEW_VERSION"

    # --- Recover from an interrupted merge (bug #2) ------------------------
    # A previous run killed mid-merge leaves MERGE_HEAD + conflict markers on
    # disk. Abort it first, or the clean-tree autocommit below would commit the
    # markers onto customer-main and push them.
    if git rev-parse -q --verify MERGE_HEAD >/dev/null 2>&1; then
        log_warn "aborting an in-progress merge left by a previous run"
        git merge --abort 2>/dev/null || git reset --hard HEAD
    fi

    # --- Clean tree ---------------------------------------------------------
    # Absorb any pending sandbox edits first, mirroring ninja-sync, so the merge
    # runs on a clean tree and those edits are preserved as customer commits.
    if [[ -n "$(git status --porcelain)" ]]; then
        git "${GIT_ID[@]}" add -A
        git "${GIT_ID[@]}" commit -q -m "ninja-upgrade: autocommit sandbox changes before upgrade"
    fi

    # --- Pre-upgrade health snapshot (for the differential gate) -----------
    # Captured on the OLD code, before the merge. Skipped when smoke is stubbed
    # (local testing) or the gate is disabled.
    if [[ "$HEALTH_CHECK" == "1" && -z "${NINJA_SMOKE_CMD:-}" ]]; then
        PRE_HEALTH="$STAGING_DIR/health-pre.json"
        health_snapshot "$PRE_HEALTH" || {
            log_warn "pre-upgrade health snapshot unavailable — health gate disabled this run"
            PRE_HEALTH=""
        }
    fi

    # --- Update baseline---------------------------------------------
    BASELINE_PREV=$(git rev-parse "$BASELINE")   # remembered for rollback (bug #1)
    update_baseline
    if git merge-base --is-ancestor "$BASELINE" HEAD; then
        # Nothing to merge — rewind the baseline to its pre-update tip so we
        # don't leave an advanced, unpushed commit behind (same invariant the
        # rollback path keeps: local ninja-upstream == last applied baseline).
        [[ -n "$BASELINE_PREV" ]] && git branch -f "$BASELINE" "$BASELINE_PREV"
        log "Baseline already merged — nothing to apply"; emit_result up_to_date; exit 0
    fi

    merge_upstream   # (tag, 3-way merge, LLM conflict resolution)
    finalize         # (smoke check → push or roll back)
}

# Only run when executed directly; sourcing (for tests) exposes the functions
# without triggering the full flow.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi

#!/usr/bin/env bash
# Run all six macrobench scenarios on every backend, into a fresh dated
# result directory, starting and stopping the local servers as needed.
#
# Usage:
#   ./scripts/run_macro_all.sh [--mini] [--max-runtime-sec N] [--outroot DIR] \
#       [--no-manage-servers] [--storage-sample-interval SEC] [--measure-storage] [backend ...]
#
#   backend ...   subset of: dolt dolt_mysql seekdb matrixone neon
#                 (default: all five)
#
# Results land in <outroot>/<backend>/ with one log per run under
# <outroot>/logs/ and a driver log with start/end stamps. The default
# outroot is run_stats/macro_<UTC date>; if that exists already, a
# _<HHMM> suffix is added so an earlier set is never overwritten.
#
# Servers: before a local backend's runs, its server is started if its port
# is not listening, and a server this script started is stopped after that
# backend's runs (and on Ctrl-C / exit). A server that was already running
# is used as is and left running. --no-manage-servers skips all of that and
# only checks reachability. Neon is remote and needs NEON_API_KEY_ORG.
#
# Storage: every run records the backend's storage size at four workflow
# points (after setup, after the workflow, after GC, after cleanup) and
# samples it every 5 s during the scenario (--storage-sample-interval
# changes that; 0 turns sampling off). --measure-storage additionally
# measures around every verb and exec on the local backends (two directory
# walks per operation, slow, and not attributable under concurrent workers)
# and on Neon fetches the project's consumption metrics after the run,
# which waits 15 min for them to appear. Off by default.
#
# Run from the repo root inside tmux: a full set takes hours.

set -uo pipefail
cd "$(dirname "$0")/.."

MINI=""
MAX_RUNTIME_SEC=14400
OUTROOT=""
MANAGE_SERVERS=true
STORAGE_FLAGS=(--storage-sample-interval 5)
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mini)              MINI="--mini"; shift ;;
        --max-runtime-sec)   MAX_RUNTIME_SEC="$2"; shift 2 ;;
        --outroot)           OUTROOT="$2"; shift 2 ;;
        --no-manage-servers) MANAGE_SERVERS=false; shift ;;
        --storage-sample-interval) STORAGE_FLAGS=(--storage-sample-interval "$2"); shift 2 ;;
        --measure-storage)   STORAGE_FLAGS+=(--measure-storage); shift ;;
        --*)                 echo "unknown option $1"; exit 1 ;;
        *)                   break ;;
    esac
done

BACKENDS=("$@")
if [[ ${#BACKENDS[@]} -eq 0 ]]; then
    BACKENDS=(dolt dolt_mysql seekdb matrixone neon)
fi
SCENARIOS=(rl_env context_mgmt multi_agent dev_agent ops_agent data_agent)

if [[ -z "$OUTROOT" ]]; then
    OUTROOT="run_stats/macro_$(date -u +%Y%m%d)${MINI:+_mini}"
    if [[ -e "$OUTROOT" ]]; then
        OUTROOT="${OUTROOT}_$(date -u +%H%M)"
    fi
fi
mkdir -p "$OUTROOT/logs"
DRIVER="$OUTROOT/logs/_driver.log"

stamp() { date -u +%FT%TZ; }
log()   { echo "=== $* $(stamp)" | tee -a "$DRIVER"; }

# --------------------------------------------------------------------------
# Local servers
# --------------------------------------------------------------------------

# Port each local backend listens on; neon is remote.
port_of() {
    case "$1" in
        dolt)       echo 5432 ;;
        dolt_mysql) echo 3306 ;;
        seekdb)     echo 2881 ;;
        matrixone)  echo 6001 ;;
        *)          echo "" ;;
    esac
}

listening() { ss -ltn 2>/dev/null | grep -q ":${1} "; }

# A real connection + query, since a port can accept before the server is
# ready to serve (SeekDB and MatrixOne bootstrap for a while after binding).
probe() {
    case "$1" in
        dolt)
            uv run python -c "import psycopg2; c=psycopg2.connect(host='127.0.0.1', port=5432, user='postgres', password='password', dbname='postgres'); c.cursor().execute('SELECT 1')" ;;
        dolt_mysql)
            uv run python -c "import pymysql; pymysql.connect(host='127.0.0.1', port=3306, user='root', password='').cursor().execute('SELECT 1')" ;;
        seekdb)
            uv run python -c "import pymysql; pymysql.connect(host='127.0.0.1', port=2881, user='root', password='').cursor().execute('SELECT 1')" ;;
        matrixone)
            uv run python -c "import pymysql; pymysql.connect(host='127.0.0.1', port=6001, user='root', password='111').cursor().execute('SELECT 1')" ;;
        *) return 1 ;;
    esac
} 2>/dev/null

# Backends whose server this script started: "<backend>=<pid>" entries.
STARTED=()

start_server() {
    local b="$1" slog="$OUTROOT/logs/${b}_server.log" pid
    case "$b" in
        dolt)
            # DoltgreSQL source build; data dir is doltgres's default.
            (cd ~/doltgresql && exec ./doltgres -data-dir ~/doltgres/databases) >> "$slog" 2>&1 &
            pid=$! ;;
        dolt_mysql)
            (cd ~/dolt && exec ~/dolt/bin/dolt sql-server --host 127.0.0.1 --port 3306 --data-dir ~/dolt/databases) >> "$slog" 2>&1 &
            pid=$! ;;
        seekdb)
            # Daemonizes itself; its own log is ~/seekdb/log/seekdb.log and
            # the pid lands in ~/seekdb/run/seekdb.pid.
            (cd ~/seekdb && ./bin/seekdb) >> "$slog" 2>&1
            sleep 2
            pid=$(cat ~/seekdb/run/seekdb.pid 2>/dev/null || echo "") ;;
        matrixone)
            # Appends to the server's own log: never truncate it, it holds
            # earlier runs' server-side history.
            (cd ~/mo/matrixone && exec ./mo-service -launch ./etc/launch/launch.toml) >> ~/mo/mo-service.log 2>&1 &
            pid=$! ;;
        *) return 1 ;;
    esac
    if [[ -z "$pid" ]]; then
        log "FAILED to start $b (no pid)"
        return 1
    fi
    STARTED+=("$b=$pid")
    log "STARTED $b server pid=$pid (log: $slog)"
    local i
    for i in $(seq 1 120); do
        if listening "$(port_of "$b")" && probe "$b"; then
            log "READY $b after ~$((i * 2))s"
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            log "FAILED $b server exited during startup (see $slog)"
            return 1
        fi
        sleep 2
    done
    log "FAILED $b server not ready after 240s"
    return 1
}

stop_server() {
    local b="$1" pid="$2" port
    port=$(port_of "$b")
    log "STOPPING $b server pid=$pid"
    kill -TERM "$pid" 2>/dev/null
    local i
    for i in $(seq 1 60); do
        if ! kill -0 "$pid" 2>/dev/null && ! listening "$port"; then
            log "STOPPED $b"
            return 0
        fi
        sleep 1
    done
    log "KILLING $b (did not stop in 60s)"
    kill -KILL "$pid" 2>/dev/null
}

stop_started() {
    local entry
    for entry in "${STARTED[@]+"${STARTED[@]}"}"; do
        stop_server "${entry%%=*}" "${entry##*=}"
    done
    STARTED=()
}
trap stop_started EXIT

# Ensure the backend is usable; starts its server when allowed.
ensure_backend() {
    local b="$1"
    if [[ "$b" == neon ]]; then
        [[ -n "${NEON_API_KEY_ORG:-}" ]] && return 0
        log "SKIP neon: NEON_API_KEY_ORG is not set"
        return 1
    fi
    if listening "$(port_of "$b")"; then
        if probe "$b"; then
            log "USING running $b server on :$(port_of "$b") (left running at the end)"
            return 0
        fi
        log "SKIP $b: port $(port_of "$b") is open but a query failed"
        return 1
    fi
    if ! $MANAGE_SERVERS; then
        log "SKIP $b: not listening on :$(port_of "$b") and --no-manage-servers given"
        return 1
    fi
    start_server "$b"
}

# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

log "RUN SET outroot=$OUTROOT backends=${BACKENDS[*]} mini=${MINI:-no} cap=${MAX_RUNTIME_SEC}s manage_servers=$MANAGE_SERVERS"
for b in "${BACKENDS[@]}"; do
    ensure_backend "$b" || continue
    for s in "${SCENARIOS[@]}"; do
        log "START $s $b"
        ./scripts/run_macrobench.sh $MINI --outdir "$OUTROOT/$b" --max-runtime-sec "$MAX_RUNTIME_SEC" \
            "${STORAGE_FLAGS[@]}" "$s" "$b" > "$OUTROOT/logs/${s}_${b}.log" 2>&1
        rc=$?
        log "END $s $b rc=$rc"
    done
    # Stop only the server this script started for this backend.
    for i in "${!STARTED[@]}"; do
        if [[ "${STARTED[$i]%%=*}" == "$b" ]]; then
            stop_server "$b" "${STARTED[$i]##*=}"
            unset 'STARTED[i]'
        fi
    done
    STARTED=("${STARTED[@]+"${STARTED[@]}"}")
done
log "ALL DONE"

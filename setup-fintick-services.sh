#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
PREFLIGHT=false
if [[ ${1:-} == --dry-run ]]; then
  DRY_RUN=true
elif [[ ${1:-} == --preflight ]]; then
  PREFLIGHT=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--dry-run|--preflight]" >&2
  exit 2
fi

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DATA_DIR=${FINTICK_DATA_DIR:-${REPO_DIR}/data}
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
RUNTIME_UNIT_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/systemd/user"
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/fintick"
ENVIRONMENT_FILE="${HOME}/.config/fintick/environment"
SUPERVISOR_CONFIG_DIR=${FINTICK_SUPERVISOR_CONFIG_DIR:-/etc/supervisor/conf.d}
PROC_ROOT=${FINTICK_PROC_ROOT:-/proc}
DASHBOARD_PORT=${FINTICK_DASHBOARD_PORT:-8137}
VERIFY_TIMEOUT=${FINTICK_VERIFY_TIMEOUT:-180}
PYTHON=${FINTICK_PYTHON:-/usr/bin/python3}
HERMES=${FINTICK_HERMES:-$(command -v hermes || true)}
HERMES_BIN_DIR=$(dirname -- "${HERMES:-/nonexistent}")
UNIT_NAMES=(
  fintick-ingest.service
  fintick-aggregate.service
  fintick-validate.service
  fintick-dashboard.service
)

if [[ ! -f "${REPO_DIR}/PRD.md" || ! -d "${REPO_DIR}/fintick" ]]; then
  echo "FinTick repository not found at ${REPO_DIR}" >&2
  exit 1
fi
if [[ ${DRY_RUN} == false && ! -x "${PYTHON}" ]]; then
  echo "Python executable not found: ${PYTHON}" >&2
  exit 1
fi
if [[ ${DRY_RUN} == false && ! -x "${HERMES}" ]]; then
  echo "Hermes executable not found. Set FINTICK_HERMES to its absolute path." >&2
  exit 1
fi
if [[ ${DRY_RUN} == false && ( -e "${ENVIRONMENT_FILE}" || -L "${ENVIRONMENT_FILE}" ) ]]; then
  if ! "${PYTHON}" - "${ENVIRONMENT_FILE}" "$(id -u)" <<'PY'
import os
import stat
import sys

path, expected_uid = sys.argv[1], int(sys.argv[2])
try:
    metadata = os.lstat(path)
except OSError:
    raise SystemExit(1)
valid = (
    stat.S_ISREG(metadata.st_mode)
    and not stat.S_ISLNK(metadata.st_mode)
    and metadata.st_uid == expected_uid
    and stat.S_IMODE(metadata.st_mode) == 0o600
)
raise SystemExit(0 if valid else 1)
PY
  then
    echo "Validation environment file must be a regular, current-user-owned file with mode 0600 (no symlinks): ${ENVIRONMENT_FILE}" >&2
    exit 1
  fi
fi
if [[ ${DRY_RUN} == false && -d "${SUPERVISOR_CONFIG_DIR}" ]]; then
  shopt -s nullglob
  SUPERVISOR_CONFIGS=("${SUPERVISOR_CONFIG_DIR}"/fintick-*.conf)
  shopt -u nullglob
  if (( ${#SUPERVISOR_CONFIGS[@]} > 0 )); then
    echo "Existing FinTick Supervisor configuration blocks the handoff: ${SUPERVISOR_CONFIGS[0]##*/}" >&2
    echo "Disable and remove all fintick-*.conf definitions before installing user-systemd services." >&2
    exit 1
  fi
fi
if [[ ${DRY_RUN} == false ]]; then
  WORKER_STATUS=0
  "${PYTHON}" "${REPO_DIR}/fintick/service_handoff.py" workers "${PROC_ROOT}" \
    || WORKER_STATUS=$?
  if (( WORKER_STATUS == 0 )); then
    echo "Existing FinTick workers are running. Stop the old service manager before installation." >&2
    echo "This safety check prevents duplicate writers and a dashboard port collision." >&2
    exit 1
  elif (( WORKER_STATUS != 1 )); then
    echo "Unable to verify whether FinTick workers are running; aborting handoff." >&2
    exit 1
  fi
fi
if [[ ${DRY_RUN} == false ]]; then
  if [[ ! ${DASHBOARD_PORT} =~ ^[0-9]+$ ]] || (( DASHBOARD_PORT < 1 || DASHBOARD_PORT > 65535 )); then
    echo "Invalid dashboard port: ${DASHBOARD_PORT}" >&2
    exit 1
  fi
  if [[ ! ${VERIFY_TIMEOUT} =~ ^[0-9]+$ ]] || (( VERIFY_TIMEOUT < 1 || VERIFY_TIMEOUT > 900 )); then
    echo "Invalid verification timeout: ${VERIFY_TIMEOUT}" >&2
    exit 1
  fi
  if ! "${PYTHON}" - "${DASHBOARD_PORT}" <<'PY'
import socket
import sys

with socket.socket() as probe:
    try:
        probe.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError:
        raise SystemExit(1)
PY
  then
    echo "Dashboard port ${DASHBOARD_PORT} is already in use; aborting handoff." >&2
    exit 1
  fi
fi
if [[ ${PREFLIGHT} == true ]]; then
  echo "FinTick service preflight passed."
  exit 0
fi

unit_body() {
  local name=$1 description=$2 command=$3 after=$4 environment_file=${5:-}
  cat <<EOF
# ${name}
[Unit]
Description=${description}
After=${after}
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
${environment_file}
Environment="PYTHONUNBUFFERED=1"
Environment="PATH=${HERMES_BIN_DIR}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin"
ExecStart=${command}
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=240
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
}

write_unit() {
  local name=$1 description=$2 command=$3 after=$4 environment_file=${5:-} body
  body=$(unit_body "${name}" "${description}" "${command}" "${after}" "${environment_file}")
  if [[ ${DRY_RUN} == true ]]; then
    printf '%s\n\n' "${body}"
  else
    prepare_unit_path "${name}"
    printf '%s\n' "${body}" >"${UNIT_DIR}/${name}"
  fi
}

is_dev_null_mask() {
  local target
  [[ -L $1 ]] || return 1
  target=$(readlink -- "$1") || return 1
  [[ ${target} == /dev/null ]]
}

unit_enabled_state() {
  local state
  state=$(systemctl --user is-enabled "$1" 2>/dev/null) || true
  case "${state}" in
    enabled|enabled-runtime|linked|linked-runtime)
      printf 'enabled\n'
      ;;
    disabled|static|indirect|generated|transient)
      printf 'disabled\n'
      ;;
    masked|masked-runtime)
      printf '%s\n' "${state}"
      ;;
    not-found)
      printf 'absent\n'
      ;;
    *)
      return 1
      ;;
  esac
}

unit_active_state() {
  local state
  state=$(systemctl --user show "$1" --property=ActiveState --value 2>/dev/null) || true
  case "${state}" in
    active)
      printf 'active\n'
      ;;
    inactive|failed)
      printf 'inactive\n'
      ;;
    *)
      return 1
      ;;
  esac
}

prepare_unit_path() {
  local name=$1 unit_path="${UNIT_DIR}/$1" runtime_path="${RUNTIME_UNIT_DIR}/$1"
  if [[ -f "${BACKUP_DIR}/units/${name}" ]]; then
    if [[ ! -f ${unit_path} || -L ${unit_path} ]]; then
      echo "Unit path changed after snapshot: ${unit_path}" >&2
      return 1
    fi
  elif [[ -f "${BACKUP_DIR}/units/${name}.persistent-masked" ]]; then
    if ! is_dev_null_mask "${unit_path}"; then
      echo "Persistent unit mask changed after snapshot: ${unit_path}" >&2
      return 1
    fi
  elif [[ -e ${unit_path} || -L ${unit_path} ]]; then
    echo "Unit path appeared after snapshot: ${unit_path}" >&2
    return 1
  fi
  rm -f -- "${unit_path}" || return 1

  if [[ -f "${BACKUP_DIR}/units/${name}.runtime-masked" ]]; then
    if ! is_dev_null_mask "${runtime_path}"; then
      echo "Runtime unit mask changed after snapshot: ${runtime_path}" >&2
      return 1
    fi
    rm -f -- "${runtime_path}" || return 1
  elif [[ -e ${runtime_path} || -L ${runtime_path} ]]; then
    echo "Runtime unit path appeared after snapshot: ${runtime_path}" >&2
    return 1
  fi
}

if [[ ${DRY_RUN} == false ]]; then
  BACKUP_DIR="${STATE_DIR}/handoff-$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -m 0755 "${DATA_DIR}" "${UNIT_DIR}"
  install -d -m 0700 "${STATE_DIR}" "${BACKUP_DIR}" "${BACKUP_DIR}/units"
  for name in "${UNIT_NAMES[@]}"; do
    unit_path="${UNIT_DIR}/${name}"
    runtime_path="${RUNTIME_UNIT_DIR}/${name}"
    has_prior_unit=false
    if [[ -L ${unit_path} ]]; then
      if ! is_dev_null_mask "${unit_path}"; then
        echo "Unsupported prior unit symlink: ${unit_path}" >&2
        exit 1
      fi
      : >"${BACKUP_DIR}/units/${name}.persistent-masked"
      has_prior_unit=true
    elif [[ -f ${unit_path} ]]; then
      cp -p "${unit_path}" "${BACKUP_DIR}/units/${name}"
      has_prior_unit=true
    elif [[ -e ${unit_path} ]]; then
      echo "Unsupported prior unit path type: ${unit_path}" >&2
      exit 1
    else
      : >"${BACKUP_DIR}/units/${name}.absent"
    fi

    if [[ -e ${runtime_path} || -L ${runtime_path} ]]; then
      if ! is_dev_null_mask "${runtime_path}"; then
        echo "Unsupported runtime unit path: ${runtime_path}" >&2
        exit 1
      fi
      if [[ -f "${BACKUP_DIR}/units/${name}.persistent-masked" ]]; then
        echo "Conflicting persistent and runtime masks for ${name}; aborting handoff." >&2
        exit 1
      fi
      : >"${BACKUP_DIR}/units/${name}.runtime-masked"
      has_prior_unit=true
    fi

    if [[ ${has_prior_unit} == true ]]; then
      if ! enabled_state=$(unit_enabled_state "${name}"); then
        echo "Cannot determine prior enabled state for ${name}; aborting handoff." >&2
        exit 1
      fi
      if [[ -f "${BACKUP_DIR}/units/${name}.persistent-masked" && ${enabled_state} != masked ]]; then
        echo "Persistent mask state mismatch for ${name}; aborting handoff." >&2
        exit 1
      fi
      if [[ -f "${BACKUP_DIR}/units/${name}.runtime-masked" && ${enabled_state} != masked-runtime ]]; then
        echo "Runtime mask state mismatch for ${name}; aborting handoff." >&2
        exit 1
      fi
      if [[ ${enabled_state} == absent ]]; then
        echo "Prior unit artifact exists but user-systemd reports ${name} absent; aborting handoff." >&2
        exit 1
      fi
      printf '%s\n' "${enabled_state}" >"${BACKUP_DIR}/units/${name}.enabled-state"
      if ! active_state=$(unit_active_state "${name}"); then
        echo "Cannot determine prior active state for ${name}; aborting handoff." >&2
        exit 1
      fi
      if [[ ( ${enabled_state} == masked || ${enabled_state} == masked-runtime ) && ${active_state} == active ]]; then
        echo "Cannot safely hand off active masked unit ${name}; aborting handoff." >&2
        exit 1
      fi
      printf '%s\n' "${active_state}" >"${BACKUP_DIR}/units/${name}.active-state"
    fi
  done
  if [[ -f "${DATA_DIR}/fintick.db" ]]; then
    "${PYTHON}" "${REPO_DIR}/fintick/service_handoff.py" snapshot \
      "${DATA_DIR}/fintick.db" "${BACKUP_DIR}/fintick.db"
  else
    : >"${BACKUP_DIR}/fintick.db.absent"
  fi

  rollback_handoff() {
    local status=$? rollback_failed=false database_recovered=true unit_artifacts_recovered=true daemon_reloaded=true units_stopped=true state journal_file
    trap - ERR
    set +e
    echo "FinTick service verification failed; restoring rollback snapshot." >&2
    if ! systemctl --user disable --now "${UNIT_NAMES[@]}" >/dev/null 2>&1; then
      echo "Rollback warning: systemctl could not disable all replacement units." >&2
    fi
    install -d -m 0700 "${BACKUP_DIR}/journals"
    for name in "${UNIT_NAMES[@]}"; do
      journal_file="${BACKUP_DIR}/journals/${name}.log"
      install -m 0600 /dev/null "${journal_file}"
      journalctl --user -u "${name}" -n 20 --no-pager >"${journal_file}" 2>&1 || true
      state=$(systemctl --user show "${name}" --property=ActiveState --value 2>/dev/null)
      if [[ ${state} != inactive && ${state} != failed ]]; then
        echo "Rollback blocked: ${name} state is ${state:-unknown}; database was not restored." >&2
        units_stopped=false
      fi
    done
    if [[ ${units_stopped} != true ]]; then
      echo "Rollback incomplete; snapshot retained at ${BACKUP_DIR}" >&2
      exit 1
    fi

    for name in "${UNIT_NAMES[@]}"; do
      if [[ -f "${BACKUP_DIR}/units/${name}" ]]; then
        if ! cp -p "${BACKUP_DIR}/units/${name}" "${UNIT_DIR}/${name}"; then
          echo "Rollback error: could not restore ${name}." >&2
          unit_artifacts_recovered=false
          rollback_failed=true
        fi
      elif [[ -f "${BACKUP_DIR}/units/${name}.persistent-masked" ]]; then
        if ! rm -f -- "${UNIT_DIR}/${name}" \
          || ! ln -s /dev/null "${UNIT_DIR}/${name}"; then
          echo "Rollback error: could not restore persistent mask for ${name}." >&2
          unit_artifacts_recovered=false
          rollback_failed=true
        fi
      else
        if ! rm -f "${UNIT_DIR}/${name}"; then
          echo "Rollback error: could not remove replacement ${name}." >&2
          unit_artifacts_recovered=false
          rollback_failed=true
        fi
      fi

      if [[ -f "${BACKUP_DIR}/units/${name}.runtime-masked" ]]; then
        if ! install -d -m 0700 "${RUNTIME_UNIT_DIR}" \
          || ! rm -f -- "${RUNTIME_UNIT_DIR}/${name}" \
          || ! ln -s /dev/null "${RUNTIME_UNIT_DIR}/${name}"; then
          echo "Rollback error: could not restore runtime mask for ${name}." >&2
          unit_artifacts_recovered=false
          rollback_failed=true
        fi
      elif ! rm -f -- "${RUNTIME_UNIT_DIR}/${name}"; then
        echo "Rollback error: could not remove replacement runtime unit ${name}." >&2
        unit_artifacts_recovered=false
        rollback_failed=true
      fi
    done
    if [[ -f "${BACKUP_DIR}/fintick.db" ]]; then
      if ! "${PYTHON}" "${REPO_DIR}/fintick/service_handoff.py" restore \
        "${BACKUP_DIR}/fintick.db" "${DATA_DIR}/fintick.db"; then
        echo "Rollback error: database restoration failed." >&2
        database_recovered=false
        rollback_failed=true
      fi
    else
      if ! rm -f "${DATA_DIR}/fintick.db" "${DATA_DIR}/fintick.db-wal" "${DATA_DIR}/fintick.db-shm"; then
        echo "Rollback error: replacement database removal failed." >&2
        database_recovered=false
        rollback_failed=true
      fi
    fi
    if ! systemctl --user daemon-reload >/dev/null 2>&1; then
      echo "Rollback error: user-systemd daemon reload failed." >&2
      daemon_reloaded=false
      rollback_failed=true
    fi
    if [[ ${database_recovered} == true && ${unit_artifacts_recovered} == true && ${daemon_reloaded} == true ]]; then
      for name in "${UNIT_NAMES[@]}"; do
        enabled_state=
        active_state=
        if [[ -f "${BACKUP_DIR}/units/${name}.enabled-state" ]]; then
          IFS= read -r enabled_state <"${BACKUP_DIR}/units/${name}.enabled-state"
        fi
        if [[ -f "${BACKUP_DIR}/units/${name}.active-state" ]]; then
          IFS= read -r active_state <"${BACKUP_DIR}/units/${name}.active-state"
        fi
        if [[ ${enabled_state} == enabled ]]; then
          if ! systemctl --user enable "${name}" >/dev/null 2>&1; then
            echo "Rollback error: could not re-enable ${name}." >&2
            rollback_failed=true
          fi
        fi
        if [[ ${active_state} == active ]]; then
          if ! systemctl --user start "${name}" >/dev/null 2>&1; then
            echo "Rollback error: could not restart ${name}." >&2
            rollback_failed=true
          fi
        fi
      done
    else
      echo "Rollback safety: prior units remain stopped because recovery prerequisites failed." >&2
    fi

    for name in "${UNIT_NAMES[@]}"; do
      if [[ -f "${BACKUP_DIR}/units/${name}.enabled-state" ]]; then
        IFS= read -r expected_enabled <"${BACKUP_DIR}/units/${name}.enabled-state"
        IFS= read -r expected_active <"${BACKUP_DIR}/units/${name}.active-state"
      elif [[ -f "${BACKUP_DIR}/units/${name}.absent" ]]; then
        expected_enabled=absent
        expected_active=inactive
      else
        echo "Rollback error: saved unit state for ${name} is incomplete." >&2
        rollback_failed=true
        continue
      fi
      if ! enabled_state=$(unit_enabled_state "${name}"); then
        echo "Rollback error: final enabled state for ${name} is unknown." >&2
        rollback_failed=true
      elif [[ ${enabled_state} != "${expected_enabled}" ]]; then
        echo "Rollback error: ${name} enabled state is ${enabled_state}, expected ${expected_enabled}." >&2
        rollback_failed=true
      fi
      if ! active_state=$(unit_active_state "${name}"); then
        echo "Rollback error: final active state for ${name} is unknown." >&2
        rollback_failed=true
      elif [[ ${active_state} != "${expected_active}" ]]; then
        echo "Rollback error: ${name} active state is ${active_state}, expected ${expected_active}." >&2
        rollback_failed=true
      fi
    done
    if [[ ${rollback_failed} == true ]]; then
      echo "Rollback incomplete; snapshot retained at ${BACKUP_DIR}" >&2
      exit 1
    fi
    echo "Rollback restored from ${BACKUP_DIR}" >&2
    exit "${status}"
  }
  trap rollback_handoff ERR
fi

write_unit fintick-ingest.service \
  "FinTick Bluesky stream ingest" \
  "${PYTHON} -m fintick ingest --database ${DATA_DIR}/fintick.db --watch --interval 900" \
  "network-online.target"
write_unit fintick-aggregate.service \
  "FinTick accountable event aggregation" \
  "${PYTHON} -m fintick aggregate --database ${DATA_DIR}/fintick.db --provider hermes --model gpt-5.6-luna --hermes-executable ${HERMES} --limit 50 --watch --interval 60" \
  "network-online.target fintick-ingest.service"
write_unit fintick-validate.service \
  "FinTick independent event validation" \
  "${PYTHON} -m fintick validate --database ${DATA_DIR}/fintick.db --watch --interval 300 --min-age 900" \
  "network-online.target fintick-aggregate.service" \
  "EnvironmentFile=-%h/.config/fintick/environment"
write_unit fintick-dashboard.service \
  "FinTick Edge Board" \
  "${PYTHON} -m fintick serve --database ${DATA_DIR}/fintick.db --host 127.0.0.1 --port ${DASHBOARD_PORT}" \
  "network-online.target"

if [[ ${DRY_RUN} == true ]]; then
  echo "Dry run only. Install with: ./setup-fintick-services.sh"
  echo "The installer uses: systemctl --user daemon-reload && systemctl --user enable --now ${UNIT_NAMES[*]}"
  exit 0
fi

BEFORE_PIPELINE=$("${PYTHON}" - "${DATA_DIR}/fintick.db" <<'PY'
import json
import sqlite3
import sys
from urllib.parse import quote

snapshot = {"backlog": 0, "latest_decision_at": None}
try:
    uri = f"file:{quote(sys.argv[1], safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "post_aggregation_decisions" in tables:
            snapshot["backlog"] = int(connection.execute(
                "SELECT COUNT(*) FROM post_aggregation_decisions "
                "WHERE state='pending' OR (state='errored' AND attempts < 3)"
            ).fetchone()[0])
            snapshot["latest_decision_at"] = connection.execute(
                "SELECT MAX(updated_at) FROM post_aggregation_decisions "
                "WHERE state IN ('assigned', 'ignored', 'errored')"
            ).fetchone()[0]
except sqlite3.Error:
    pass
print(json.dumps(snapshot, separators=(",", ":")))
PY
)
systemctl --user daemon-reload
systemctl --user enable --now "${UNIT_NAMES[@]}"
for name in "${UNIT_NAMES[@]}"; do
  systemctl --user is-active --quiet "${name}"
done
EXPECTED_DATABASE_IDENTITY=$("${PYTHON}" - "${DATA_DIR}/fintick.db" "${VERIFY_TIMEOUT}" <<'PY'
import hashlib
import os
import sys
import time

database, timeout = sys.argv[1], int(sys.argv[2])
deadline = time.monotonic() + timeout
while True:
    try:
        metadata = os.stat(database)
        value = f"{metadata.st_dev}:{metadata.st_ino}".encode("ascii")
        print(hashlib.sha256(value).hexdigest())
        raise SystemExit(0)
    except FileNotFoundError:
        if time.monotonic() >= deadline:
            print("Operational database was not created before timeout.", file=sys.stderr)
            raise SystemExit(1)
        time.sleep(0.1)
PY
)
"${PYTHON}" - \
  "${DATA_DIR}/fintick.db" "${DASHBOARD_PORT}" "${VERIFY_TIMEOUT}" \
  "${BEFORE_PIPELINE}" "${EXPECTED_DATABASE_IDENTITY}" <<'PY'
import json
import sqlite3
import sys
import time
import urllib.request

database, port, timeout = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
before = json.loads(sys.argv[4])
expected_database_identity = sys.argv[5]
url = f"http://127.0.0.1:{port}/api/feed"
deadline = time.monotonic() + timeout
last_error = "dashboard did not respond"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.load(response)
        pipeline = payload["pipeline"]
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            database_posts = int(connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
        posts = int(pipeline["posts"])
        accounted = int(pipeline["accounted"])
        backlog = int(pipeline["backlog"])
        terminal_errors = int(pipeline["terminal_errors"])
        if pipeline["database_identity"] != expected_database_identity:
            raise ValueError("database identity mismatch")
        if posts != database_posts:
            raise ValueError(
                f"dashboard/database mismatch: API posts={posts}, database posts={database_posts}"
            )
        if accounted + backlog + terminal_errors != posts:
            raise ValueError("pipeline accounting does not conserve all posts")
        if (
            int(before["backlog"]) > 0
            and backlog >= int(before["backlog"])
            and pipeline.get("latest_decision_at") == before.get("latest_decision_at")
        ):
            raise ValueError(
                f"pipeline backlog did not move from {before['backlog']} posts"
            )
        print(
            f"Verified dashboard/API identity: posts={posts} backlog={backlog} "
            f"terminal_errors={terminal_errors}"
        )
        raise SystemExit(0)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
        last_error = str(error)
        time.sleep(1)
print(f"Dashboard verification failed: {last_error}", file=sys.stderr)
raise SystemExit(1)
PY
for name in "${UNIT_NAMES[@]}"; do
  systemctl --user is-active --quiet "${name}"
done
install -d -m 0700 "${BACKUP_DIR}/journals"
for name in "${UNIT_NAMES[@]}"; do
  journal_file="${BACKUP_DIR}/journals/${name}.log"
  install -m 0600 /dev/null "${journal_file}"
  journalctl --user -u "${name}" -n 20 --no-pager >"${journal_file}"
done
trap - ERR
echo "Installed and started rootless FinTick user services in ${UNIT_DIR}."
echo "Rollback snapshot: ${BACKUP_DIR}"
echo "Status: systemctl --user status ${UNIT_NAMES[*]}"
echo "Logs: journalctl --user -u 'fintick-*' --since today"
echo "Board: http://127.0.0.1:${DASHBOARD_PORT}"

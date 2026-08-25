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
DATA_DIR="${REPO_DIR}/data"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
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
if [[ ${DRY_RUN} == false ]] && pgrep -af 'python[^ ]* -m fintick ((ingest|aggregate|validate).*--watch|serve( |$))' >/dev/null; then
  echo "Existing FinTick workers are running. Stop the old service manager before installation." >&2
  echo "This safety check prevents duplicate writers and a dashboard port collision." >&2
  exit 1
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
    printf '%s\n' "${body}" >"${UNIT_DIR}/${name}"
  fi
}

if [[ ${DRY_RUN} == false ]]; then
  install -d -m 0755 "${DATA_DIR}" "${UNIT_DIR}"
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
  "${PYTHON} -m fintick serve --database ${DATA_DIR}/fintick.db --host 127.0.0.1 --port 8137" \
  "network-online.target"

if [[ ${DRY_RUN} == true ]]; then
  echo "Dry run only. Install with: ./setup-fintick-services.sh"
  echo "The installer uses: systemctl --user daemon-reload && systemctl --user enable --now ${UNIT_NAMES[*]}"
  exit 0
fi

systemctl --user daemon-reload
systemctl --user enable --now "${UNIT_NAMES[@]}"
echo "Installed and started rootless FinTick user services in ${UNIT_DIR}."
echo "Status: systemctl --user status ${UNIT_NAMES[*]}"
echo "Logs: journalctl --user -u 'fintick-*' --since today"
echo "Board: http://127.0.0.1:8137"

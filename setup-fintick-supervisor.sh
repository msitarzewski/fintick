#!/usr/bin/env bash
set -euo pipefail

# Run this script with sudo. FinTick itself always runs as the unprivileged
# michael user; root is needed only to install Supervisor configuration.
if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer with: sudo ./setup-fintick-supervisor.sh" >&2
  exit 1
fi

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RUN_USER=michael
PYTHON=/usr/bin/python3
CONF_DIR=/etc/supervisor/conf.d
DATA_DIR="${REPO_DIR}/data"
LOG_DIR=/var/log/fintick

if [[ ! -f "${REPO_DIR}/PRD.md" || ! -d "${REPO_DIR}/fintick" ]]; then
  echo "FinTick repository not found at ${REPO_DIR}" >&2
  exit 1
fi
if ! id "${RUN_USER}" >/dev/null 2>&1; then
  echo "Required user ${RUN_USER} does not exist" >&2
  exit 1
fi

# Supervisor opens logs as root. Keep this directory root-owned so a compromised
# worker cannot replace a log path with a symlink to another root-writable file.
install -d -o "${RUN_USER}" -g "${RUN_USER}" "${DATA_DIR}"
install -d -o root -g root -m 0755 "${LOG_DIR}"

write_program() {
  local name=$1 command=$2 startsecs=$3 stopwaitsecs=$4
  cat >"${CONF_DIR}/${name}.conf" <<EOF
[program:${name}]
process_name=%(program_name)s
command=${command}
directory=${REPO_DIR}
user=${RUN_USER}
autostart=true
autorestart=true
startsecs=${startsecs}
stopwaitsecs=${stopwaitsecs}
stopsignal=TERM
redirect_stderr=true
stdout_logfile=${LOG_DIR}/${name}.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
environment=PYTHONUNBUFFERED="1",HOME="/home/${RUN_USER}"
EOF
}

# Aggregation makes one bounded local-model call per 15-minute cycle. Validation
# rechecks unconfirmed events every five minutes, with per-event caching.
write_program fintick-ingest "${PYTHON} -m fintick ingest --database ${DATA_DIR}/fintick.db --watch --interval 900" 5 300
write_program fintick-aggregate "${PYTHON} -m fintick aggregate --database ${DATA_DIR}/fintick.db --watch --interval 900" 5 360
write_program fintick-validate "${PYTHON} -m fintick validate --database ${DATA_DIR}/fintick.db --watch --interval 300 --min-age 900" 5 90
write_program fintick-dashboard "${PYTHON} -m fintick serve --database ${DATA_DIR}/fintick.db --host 127.0.0.1 --port 8137" 5 15

chown root:root "${CONF_DIR}"/fintick-*.conf
chmod 0644 "${CONF_DIR}"/fintick-*.conf

echo "Installed four FinTick Supervisor programs in ${CONF_DIR}."
echo "Run: sudo supervisorctl reread && sudo supervisorctl update"
echo "Then open: http://127.0.0.1:8137"

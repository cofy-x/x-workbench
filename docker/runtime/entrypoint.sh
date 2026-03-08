#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/app}"
TOOLS_DIR="${TOOLS_DIR:-${APP_ROOT}/tools}"
TOOL_BASE_PORT="${TOOL_BASE_PORT:-18001}"
RUNTIME_ROOT="${RUNTIME_ROOT:-/tmp/x-workbench-runtime}"

LANDING_DIR="${RUNTIME_ROOT}/landing"
NGINX_DIR="${RUNTIME_ROOT}/nginx"
SUPERVISOR_DIR="${RUNTIME_ROOT}/supervisor"

NGINX_TEMPLATE="${APP_ROOT}/docker/nginx/nginx.conf.template"
SUPERVISOR_TEMPLATE="${APP_ROOT}/docker/supervisor/supervisord.conf.template"
LANDING_TEMPLATE="${APP_ROOT}/docker/landing/index.html.template"

NGINX_ROUTES_FILE="${NGINX_DIR}/tool_routes.conf"
SUPERVISOR_PROGRAMS_FILE="${SUPERVISOR_DIR}/tool_programs.conf"

NGINX_CONF="${NGINX_DIR}/nginx.conf"
SUPERVISOR_CONF="${SUPERVISOR_DIR}/supervisord.conf"

mkdir -p "${LANDING_DIR}" "${NGINX_DIR}" "${SUPERVISOR_DIR}" "${APP_ROOT}/generated" "${HF_HOME:-/data/hf}" "${XDG_CACHE_HOME:-/data/cache}"

if [[ ! -f "${NGINX_TEMPLATE}" ]]; then
    echo "[entrypoint] missing nginx template: ${NGINX_TEMPLATE}" >&2
    exit 1
fi

if [[ ! -f "${SUPERVISOR_TEMPLATE}" ]]; then
    echo "[entrypoint] missing supervisor template: ${SUPERVISOR_TEMPLATE}" >&2
    exit 1
fi

if [[ ! -f "${LANDING_TEMPLATE}" ]]; then
    echo "[entrypoint] missing landing template: ${LANDING_TEMPLATE}" >&2
    exit 1
fi

mapfile -t TOOL_APPS < <(find "${TOOLS_DIR}" -mindepth 2 -maxdepth 2 -type f -name app.py | sort)

declare -a TOOLS=()
for app in "${TOOL_APPS[@]}"; do
    tool_name="$(basename "$(dirname "${app}")")"
    if [[ "${tool_name}" == "_shared" ]]; then
        continue
    fi
    TOOLS+=("${tool_name}")
done

if [[ "${#TOOLS[@]}" -eq 0 ]]; then
    echo "[entrypoint] no tools discovered under ${TOOLS_DIR}" >&2
    exit 1
fi

: > "${NGINX_ROUTES_FILE}"
: > "${SUPERVISOR_PROGRAMS_FILE}"

LANDING_ITEMS_FILE="${LANDING_DIR}/tool_items.html"
: > "${LANDING_ITEMS_FILE}"

port="${TOOL_BASE_PORT}"
for tool_name in "${TOOLS[@]}"; do
    cat >> "${NGINX_ROUTES_FILE}" <<NGINX_ROUTE
        location = /tools/${tool_name} {
            return 301 /tools/${tool_name}/;
        }

        location /tools/${tool_name}/ {
            proxy_http_version 1.1;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_set_header X-Forwarded-Prefix /tools/${tool_name};
            proxy_pass http://127.0.0.1:${port}/;
        }

NGINX_ROUTE

    cat >> "${SUPERVISOR_PROGRAMS_FILE}" <<SUP_PROGRAM
[program:tool_${tool_name}]
command=${APP_ROOT}/.venv/bin/python ${APP_ROOT}/tools/${tool_name}/app.py --host 127.0.0.1 --port ${port}
directory=${APP_ROOT}
autostart=true
autorestart=true
startsecs=1
priority=20
environment=TOOLS_WORKSPACE_ROOT="${APP_ROOT}",HF_HOME="${HF_HOME:-/data/hf}",XDG_CACHE_HOME="${XDG_CACHE_HOME:-/data/cache}",PYTHONUNBUFFERED="1"
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0

SUP_PROGRAM

    printf '      <li><a href="/tools/%s/">%s</a><span>internal:127.0.0.1:%s</span></li>\n' "${tool_name}" "${tool_name}" "${port}" >> "${LANDING_ITEMS_FILE}"

    port="$((port + 1))"
done

awk -v items_file="${LANDING_ITEMS_FILE}" '
    /__TOOL_ITEMS__/ {
        while ((getline line < items_file) > 0) {
            print line
        }
        close(items_file)
        next
    }
    { print }
' "${LANDING_TEMPLATE}" > "${LANDING_DIR}/index.html"

sed "s|__LANDING_ROOT__|${LANDING_DIR}|g" "${NGINX_TEMPLATE}" | \
awk -v routes_file="${NGINX_ROUTES_FILE}" '
    /__TOOL_ROUTES__/ {
        while ((getline line < routes_file) > 0) {
            print line
        }
        close(routes_file)
        next
    }
    { print }
' > "${NGINX_CONF}"

sed "s|__NGINX_CONF__|${NGINX_CONF}|g" "${SUPERVISOR_TEMPLATE}" | \
awk -v programs_file="${SUPERVISOR_PROGRAMS_FILE}" '
    /__TOOL_PROGRAMS__/ {
        while ((getline line < programs_file) > 0) {
            print line
        }
        close(programs_file)
        next
    }
    { print }
' > "${SUPERVISOR_CONF}"

printf '[entrypoint] discovered tools (%d): %s\n' "${#TOOLS[@]}" "${TOOLS[*]}"
printf '[entrypoint] starting supervisor with config: %s\n' "${SUPERVISOR_CONF}"

exec /usr/bin/supervisord -c "${SUPERVISOR_CONF}"

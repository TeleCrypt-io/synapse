#!/usr/bin/env bash

set -euo pipefail

poetry_user_bin="$(python -m site --user-base)/bin"
export PATH="${poetry_user_bin}:${PATH}"
echo "${poetry_user_bin}" >> "${GITHUB_PATH}"

python -m pip install --user "poetry==2.4.1"

poetry_args=(install --no-interaction)
if [[ -n "${POETRY_EXTRAS:-}" ]]; then
    poetry_args+=(--extras "${POETRY_EXTRAS}")
fi
if [[ "${POETRY_INSTALL_PROJECT:-true}" == "false" ]]; then
    poetry_args+=(--no-root)
fi

python -m poetry check --lock
python -m poetry "${poetry_args[@]}"

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
# Thrift's upstream setup.cfg requests optimized bytecode during source builds,
# which leaves __pycache__ files in the wheel. Keep the override scoped to this
# dependency installation; it applies to any setuptools source build spawned by
# this command, and DIST_EXTRA_CONFIG is inherited by PEP 517 backends.
DIST_EXTRA_CONFIG="${PWD}/.ci/setuptools-install.cfg" \
POETRY_INSTALLER_NO_BINARY=thrift \
    python -m poetry "${poetry_args[@]}"

#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "usage: setup_rust_toolchain.sh TOOLCHAIN [RUSTUP-TOOLCHAIN-INSTALL-ARG ...]" >&2
    exit 64
fi

toolchain="$1"
shift
rustup set profile minimal
rustup toolchain install "$toolchain" --no-self-update "$@"
rustup default "$toolchain"

#!/usr/bin/env bash
# Fetch the Spider_XHS SDK and apply this project's fixes.
# Usage: bash scripts/setup_sdk.sh
set -e
cd "$(dirname "$0")/.."

if [ -d Spider_XHS/.git ]; then
    echo "[skip] Spider_XHS already present."
else
    echo "[1/2] Cloning Spider_XHS ..."
    git clone --depth 1 https://github.com/cv-cat/Spider_XHS.git Spider_XHS
fi

echo "[2/2] Applying fixes patch ..."
cd Spider_XHS
if git apply --reverse --check ../patches/spider_xhs_fixes.patch 2>/dev/null; then
    echo "[skip] Patch already applied."
else
    git apply ../patches/spider_xhs_fixes.patch
    echo "[done] Patch applied."
fi
cd ..

echo
echo "All set. Next: create a venv and install dependencies, see README.md"

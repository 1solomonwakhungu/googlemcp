#!/usr/bin/env bash
# Offline verification for infra/terraform. Needs no Google credentials:
# init runs with -backend=false and tests use a mock provider.
# Usage: infra/terraform/scripts/verify.sh   (from any directory)
set -euo pipefail

TF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TF_DIR"

echo "== terraform fmt -check -recursive"
terraform fmt -check -recursive

for dir in . bootstrap; do
  echo "== terraform init -backend=false && terraform validate ($dir)"
  terraform -chdir="$dir" init -backend=false -input=false >/dev/null
  terraform -chdir="$dir" validate
done

echo "== terraform test"
terraform test

if command -v tflint >/dev/null 2>&1; then
  echo "== tflint"
  tflint --init >/dev/null
  tflint --recursive --config "$TF_DIR/.tflint.hcl"
else
  echo "== tflint not installed; skipped (CI installs it, see docs/terraform.md)"
fi

echo "verify: OK"

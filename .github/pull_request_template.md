## Summary

<!-- What changes and why. Link the issue it closes, for example "Closes #12". -->

## Changes

-

## OAuth scopes and Google APIs

<!-- List any scope, API, or catalog/services.json change. Write "None" if there is none. -->

## Testing

<!-- Commands you ran and their results. -->

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest`
- [ ] `infra/terraform/scripts/verify.sh` (only if `infra/` changed)

## Checklist

- [ ] No credentials, tokens, personal data, or Terraform state are included.
- [ ] Docs and CHANGELOG.md are updated where behavior changes.

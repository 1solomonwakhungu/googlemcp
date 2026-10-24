# Contributing

Bug reports, fixes, and new product adapters are welcome. Open an issue
before starting a large change so we can agree on the approach.

Do not report security problems in public issues; see
[SECURITY.md](SECURITY.md).

## Setup

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). Terraform 1.9+,
tflint, and gitleaks are needed only for the infrastructure and secret-scan
checks.

```sh
uv sync
uv run --with pre-commit pre-commit install
```

Development details (module layout, configuration precedence, testing
patterns): [docs/development.md](docs/development.md).

## Checks

Every pull request must pass these, which CI also runs:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python scripts/gen_inventory.py --check
infra/terraform/scripts/verify.sh      # if you touched infra/ or the catalog
gitleaks git .
```

`scripts/acceptance/preauth.sh` runs all of them and prints a summary table.

The test suite is offline. Tests that need a real Google account are marked
`@pytest.mark.live` and run only with `GOOGLEMCP_LIVE=1`; run them only
against your own account, and never commit their output.

## Adding or changing a product

1. Add or edit the entry in `catalog/services.json` (key, title,
   `api_services`, `default_enabled`, `risk`). Terraform reads the same file.
2. Write the adapter in `src/googlemcp/services/<key>.py` following
   [docs/architecture.md](docs/architecture.md#adapter-contract-summary):
   read scopes by default, write tools marked `write=True`, and delete,
   trash, or send tools also marked `destructive=True`. Adapters never
   import FastMCP.
3. Add tests with `googlemcp.testing.FakeContext` and canned Google
   responses. `tests/test_contract.py` checks the adapter against the
   catalog.
4. Add a read-only probe in `src/googlemcp/probes.py` if the product has a
   cheap read call.
5. Regenerate the inventory: `uv run python scripts/gen_inventory.py`.
6. Add the product row to the table in `README.md`;
   `tests/test_readme.py` checks it against the catalog.

New products should start read-only. Products that need a Workspace
account, create server-side state, or can run code should be
`default_enabled: false`.

## Rules

- Never commit OAuth client secrets, tokens, downloaded credential JSON,
  Terraform state, or `*.tfvars` with real values. Test fixtures use short,
  obviously fake values. The pre-commit hooks and CI run gitleaks.
- Keep the server local and stdio only. Changes that add a network
  listener, HTTP transport, or hosted deployment will not be accepted.
- No em dashes in tracked text (a pre-commit hook enforces this).
- Docs are written for any user: use `you@example.com`, `PROJECT_ID`, and
  `/path/to/googlemcp` as placeholders.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for
  commit messages, for example `feat(tasks): add move tool` or
  `fix(gmail): handle empty threads`.
- Add a line to [CHANGELOG.md](CHANGELOG.md) under "Unreleased" for
  user-visible changes.

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE) and that you will follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

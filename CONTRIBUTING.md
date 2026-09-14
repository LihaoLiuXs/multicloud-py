# Contributing to MultiCloud Python SDK

Thank you for your interest in contributing. This guide describes how to propose changes and what maintainers expect from contributions.

## Governance

MultiCloud Python SDK is a Salesforce-sponsored open source project. The goal is to grow its contributor and user community while maintaining a consistent, cloud-agnostic API. Salesforce employees hold repository administration rights and make final decisions about accepted contributions.

## Before You Start

Search the [existing issues](https://github.com/salesforce/multicloud-py/issues) before opening a new one. For significant features or API changes, open an issue first so the approach and cross-provider behavior can be discussed before implementation begins.

## Development Setup

The repository is a [uv](https://docs.astral.sh/uv/) workspace. Clone your fork and install the workspace dependencies:

```bash
git clone https://github.com/<your-github-user>/multicloud-py.git
cd multicloud-py
uv sync --frozen
```

Run the unit tests and replay-mode conformance tests before submitting a pull request:

```bash
uv run pytest tests/ -m "not conformance"
uv run pytest tests/ -m conformance --record-mode=none
uv run black --check packages tests
uv run isort --check-only packages tests
```

Conformance tests validate consistent behavior across supported providers. Recording new cassettes requires credentials for the relevant cloud provider; never commit credentials or unsanitized recordings.

## Issues and Feature Requests

- Report reproducible bugs through [GitHub Issues](https://github.com/salesforce/multicloud-py/issues/new).
- Describe the expected cloud-agnostic behavior and affected providers.
- Include a minimal reproduction when possible.
- Discuss provider-specific features before implementation. Public APIs should remain implementable consistently across supported providers.

## Pull Requests

1. Create a focused branch in your fork.
2. Keep commits atomic and use descriptive commit messages.
3. Add or update unit and conformance tests for behavior changes.
4. Update user-facing documentation when APIs or behavior change.
5. Run the validation commands above.
6. Open a pull request against `main` and link the relevant issue.
7. Sign the Salesforce Contributor License Agreement when prompted.

Keep pull requests focused and avoid unrelated formatting or refactoring changes. All changes require peer review before merge.

## Contributor License Agreement

Contributors must sign the [Salesforce Contributor License Agreement](https://cla.salesforce.com/sign-cla). You only need to sign it once for Salesforce open source projects.

## Code of Conduct

All contributors must follow the project [Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contribution will be licensed under the [Apache License 2.0](LICENSE.txt).

## Summary

Describe the change and link its issue, if applicable.

## Validation

- Describe the unit and replay-conformance tests run and their results.
- Identify affected providers and confirm behavior remains cloud-agnostic.
- Update documentation and disclose any public API or behavior changes.
- Never include credentials or unsanitized recordings. Live tests are opt-in;
  distinguish live results from replay, mocked, and skipped tests.

## PR title and release notes

PRs are squash-merged using the PR title. Use `<type>[(scope)][!]: <description>`.

Examples: `fix(blob): preserve metadata`, `feat(sts): add an operation`,
`docs: clarify setup`, or `feat!: change a public interface`.

Allowed types: `feat`, `fix`, `perf`, `refactor`, `revert`, `docs`, `style`,
`test`, `build`, `ci`, `chore`. Explain breaking changes in the PR body.

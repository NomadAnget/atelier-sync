# Atelier PR Check and Promotion

`atelier-sync` receives PR notifications from Atelier repositories, runs the kernel checks against the affected variants, and promotes eligible changes.

## Pull request flow

For a child-repository PR notification, the workflow verifies that the PR is open, non-draft, targets that repository's `master` branch, and still has the notified head commit. It checks the GitHub-generated PR merge candidate and verifies its tree before promotion.

The check assembles each consuming variant with the proposed child commit and runs:

```sh
python -I -m atelier_core.discipline <variant>/src
python -m src.main --check
```

If a check fails, the child PR is not merged and variant pointers are not updated. The central run summary is posted to the source PR when its repository and PR number are supplied.

If all checks pass, the workflow merges the child PR, then updates the gitlink in each affected variant and merges the generated pointer PR. It verifies that each variant's `master` points to the merged child commit.

## Trigger types

- `atelier-submodule-pr` (also accepts the legacy `atelier_submodule_pr`): validate, check, and promote a notified child PR.
- `atelier_check`: run checks only; it does not merge a PR or update variant pointers.
- `workflow_dispatch`: manually run checks for a repository and full commit SHA; it does not promote changes.

The default branch is `main`. Source repositories send notifications through `repository_dispatch`; GitHub does not send another repository's PR events to this repository automatically.

## Token and execution

The workflow uses the `GH_SYNC_TOKEN` Actions secret to read the required Git sources, post source-PR summaries, merge eligible child PRs, and update/merge variant pointer PRs. The token must have the required access to the participating repositories.

Checks run on GitHub-hosted runners against project code. This is intended for trusted Atelier repository changes, not as a sandbox for arbitrary untrusted code. The workflow does not upload source checkouts as artifacts; GitHub Actions logs and summaries contain check output and status.

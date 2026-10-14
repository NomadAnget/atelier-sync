# Commit assembly check

One script, one GitHub Actions workflow. No merge bot, receipt database, fixed-SHA
activation, Docker, source artifacts or synchronization system.

## What it does

Input: changed GitHub repository and a full commit SHA. It checks the three known
Atelier variants, reads their `.gitmodules`, and checks every variant consuming
that repository. A variant commit is checked directly. A kernel commit is used
as the kernel for all selected variants.

For each affected variant it clones the complete Git repository and recursive
submodules at the variant's current default-branch revision, then checks out the
submitted SHA in the affected submodule. Git history and commit metadata remain
available. A local-only assembly commit records changed gitlinks; nothing is
pushed. The report records the variant base, assembly, kernel and submodule
commits. Unrelated submodules keep their original gitlink revisions.

A fresh Python 3.11 virtual environment installs the checked-out kernel and
`ollama`, the lightweight import dependency needed by the current declaration
checks. It does not install the CUDA/model stack or change project lock files.
Both requested checks run even when the first reports a violation:

```sh
.venv/bin/python -I -m atelier_core.discipline /checkout/src
.venv/bin/python -m src.main --check
```

The second command runs the kernel's existing declaration/bus/storage assembly
self-test (`fill_noop=True`). It does not prove real business handler execution,
model inference, production startup, or end-to-end media processing.

## Results

The process exits nonzero on preparation failure, no matching variants, or any
failed check. `WORK/result.json` contains exact commits, commands and exit codes;
each variant runtime directory contains `constitution.log` and `assembly.log`.
GitHub also shows the result in job logs and the job summary. No source is
uploaded as an artifact. All check commands run without token environment
variables or Git credential configuration; this remains owner-code CI, not a
sandbox for hostile code.

## Calling it

```sh
uv run --no-project --python 3.11 python check_commit.py \
  --repository NomadAnget/atelier-publishers --sha FULL_COMMIT_SHA \
  --work ./tmp/new-check
```

Use `--variant atelier-reel-solo` to select one variant; the default checks all
affected variants. The work directory must not already exist. The optional
`--prepared-python` flag is for local verification only; GitHub always creates a
fresh environment.

The workflow uses the existing `GH_SYNC_TOKEN` only to read private Git sources;
it does not need Actions-variable, status-write, merge or Forgejo permissions.
Use **Actions > Check submitted commit > Run workflow**, or dispatch:

```json
{"event_type":"atelier_check","client_payload":{"repository":"NomadAnget/atelier-publishers","sha":"FULL_COMMIT_SHA"}}
```

GitHub does not deliver another repository's push events to this repository
automatically. The source repository needs to send this notification once per
push (or an existing webhook needs to send it). This repository only implements
the receiving check; it does not install workflows in other repositories or
report a PR status automatically. A GitHub job result is returned, not a merge
authorization or a new coordination protocol.

Minimal source-repository notification (install only where wanted; the existing
token must be available in that source repository and already able to dispatch
to atelier-sync):

```yaml
name: Notify commit check
on: [push]
permissions: {}
jobs:
  notify:
    if: github.actor == github.repository_owner
    runs-on: ubuntu-latest
    steps:
      - env:
          GH_TOKEN: ${{ secrets.GH_SYNC_TOKEN }}
          SOURCE_REPO: ${{ github.repository }}
          SOURCE_SHA: ${{ github.sha }}
        run: |
          gh api --method POST repos/NomadAnget/atelier-sync/dispatches \
            -f event_type=atelier_check \
            -f "client_payload[repository]=$SOURCE_REPO" \
            -f "client_payload[sha]=$SOURCE_SHA"
```

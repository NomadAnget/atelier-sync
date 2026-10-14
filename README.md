# Atelier machine check

Only machine_check.py and a GitHub Actions workflow template are maintained here.
The old coordinator, receipts, registry, manual SHA activation and automatic merge/sync code have been removed.

Copy machine_check.py to each variant root and machine-check.yml to .github/workflows/machine-check.yml.
GitHub runs the trusted default-branch checker on the current PR merge candidate.
The required check remains atelier/trusted-ci. GitHub branch protection decides whether merging is permitted.

The script checks Python syntax and calls the existing atelier-core discipline checker.
It does not run models, arbitrary domain tests, merge code, publish receipts, or synchronize repositories.
Existing GH_SYNC_TOKEN is used only to check out the private kernel and submodules; it is not put in the check step environment and Git credentials are not persisted.
Only same-repository PRs authored by the owner are eligible. Forks do not receive private-source preparation.
The kernel checker imports candidate header/schema declarations: this is owner-code CI, not a sandbox for malicious submissions.
No source artifact or cache is uploaded.

The first installation must be merged into the default branch before pull_request_target events can invoke the new workflow.
Existing main branch protection is retained; missing checks are not bypassed by this template.

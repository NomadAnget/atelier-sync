# Automatic control revision

The private atelier-sync main branch is the control authority. Public CI and
metadata publication check out main, then record the actual checkout commit in
the job environment. Reconciliation records its own workflow commit. No Actions
variable is written or manually advanced when the control program changes.

COORDINATOR_TRUSTED_SHA remains an internal per-run value for receipt consistency,
not a repository variable or activation switch. Existing head/base/merge identity,
fork rejection, default-branch execution, workflow blob verification, environments,
credential-free candidate environment and no-source-artifact rules are unchanged.

If control main advances between CI and publication/admission, the old receipt
is rejected. Reconciliation requests a new combination under the new revision;
no stale receipt is accepted just to finish a merge. Existing activation and
automatic-merge scopes are unchanged by this change.

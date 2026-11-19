# PostgreSQL deployment on this branch

Use the [manual FOCUS deployment guide](manual-focus-deployment.md).

The original private Premium ACR, private build VM implementation and
managed-identity image pulls are unchanged. PostgreSQL serving, isolated
assessment workers and the dedicated database bootstrap remain in use.

An organization-supplied private Docker build host is an advanced alternative:
it must reach the private registry and publish reviewed immutable digests.
It does not remove schema bootstrap, private dependencies, hosting IAM approvals,
or source/image verification requirements. Do not use a public ACR/network
exception to bypass private build access.

Native exports, automatic cost-history pulls, user-sign-in registrations and
target subscription role-assignment stages are not part of this branch.

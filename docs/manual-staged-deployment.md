# Manual FOCUS staged deployment

This branch replaces Entra user sign-in, scheduled native exports and target IAM
onboarding with anonymous upload-specific workspaces and optional Reader SP
enrichment.

Follow the [current deployment guide](manual-focus-deployment.md), including its
individual-stage approval table. Valid stages are Initialize, Foundation, Build,
Bootstrap, Release, Verify and StopBuilder. Identity and Permissions are retired.

Use a new environment. The PostgreSQL bootstrap and private ACR/private-builder
requirements remain mandatory. No migration of old shared reports or automatic
shutdown/removal of old scheduled processors is implied.

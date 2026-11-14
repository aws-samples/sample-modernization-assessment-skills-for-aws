# Installation and authentication

Reference date: 2026-10-16. Recheck current official installers and supported operating systems before installation.

## AWS CLI v2

Use official v2 installation packages; `pip install awscli` does not install v2.

| OS | Installation | Verification |
|---|---|---|
| macOS | Download official AWSCLIV2.pkg, verify its signature, use the GUI installer or the appropriate system installer command | executable path and aws --version |
| Linux x86_64 | Download awscli-exe-linux-x86_64.zip and signature, verify, extract, run aws/install | executable path and version |
| Linux aarch64 | Use the aarch64 package, not x86_64 | executable path and version |
| Windows | Install official AWSCLIV2.msi and reopen the shell for PATH changes | Get-Command aws and aws --version |

Linux supports user-writable `--install-dir`/`--bin-dir`. Verify the actual existing installation before `--update`. Preserve TLS verification; configure the organization's trusted CA/proxy when needed.

## IAM Identity Center

```bash
aws configure sso --profile assessment
aws sso login --profile assessment
aws configure set region ap-northeast-2 --profile assessment
```

The SSO authentication region may differ from the workload region. Verify each profile's account and role even when sharing an SSO session. Read only relevant non-secret configuration fields.

```ini
[profile assessment]
sso_session = corporate
sso_account_id = 123456789012
sso_role_name = ReadOnlyAssessment
region = ap-northeast-2
output = json

[sso-session corporate]
sso_start_url = https://example.awsapps.com/start
sso_region = ap-northeast-2
sso_registration_scopes = sso:account:access
```

These are format examples. Preserve existing AssumeRole role_arn/source_profile/credential_source chains.

## Authentication isolation

Explicit profile verification removes ambient static keys and AWS_PROFILE in the child process; AWS CLI resolves the profile's role chain. Environment mode uses only credentials in the process and disables configuration/credential files and container/instance-role fallback. An AWS_PROFILE environment variable is not interchangeable with an explicit CLI profile; verify identity with STS.

Ignore configured nonstandard AWS endpoints in assessment helpers. No test-endpoint override is provided. Required access consists of STS and scoped List/Describe/selected Get operations; do not create broad administrative grants.

## Sources

- https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
- https://docs.aws.amazon.com/cli/latest/userguide/getting-started-version.html
- https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html
- https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-envvars.html
- https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-role.html
- https://docs.aws.amazon.com/cli/latest/reference/sts/get-caller-identity.html

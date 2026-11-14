# Running the skills in AWS CloudShell

CloudShell gives you an authenticated shell in the account you are signed in to, with the AWS CLI v2,
Git, Node.js, Python and (since late 2026) Kiro CLI preinstalled, plus a persistent 1 GB home
directory. The notes below cover the differences that matter for these skills. Checked on
2026-10-30 against the Amazon Linux 2023 CloudShell image (Python 3.13.16, aws-cli 2.38.6,
kiro-cli 2.22.1, node 20); verify versions on your shell because the image changes.

## 1. Install

```bash
curl -fsSL https://github.com/aws-samples/sample-modernization-assessment-skills-for-aws/releases/latest/download/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
mkdir -p ~/assessment && cd ~/assessment
moda-skill config --harness kiro && moda-skill doctor
```

While the repository is private, upload the release zip with **Actions > Upload file** (one file per
upload; the picker is the browser's native dialog) together with its `SHA256SUMS`, then
`sh install.sh --from ~/moda-skill-2.2.1.zip`. Or point the installer at a mirror:
`MODA_SKILL_BASE_URL=https://<host>/path sh install.sh` (the path must serve `VERSION`,
`moda-skill-<version>.zip` and `SHA256SUMS`). The home directory persists, so the install survives new
sessions; `moda-skill update` later swaps in a newer release.

## 2. Prerequisites

| Need | CloudShell default | What to do |
|---|---|---|
| Python 3.11+ (helpers use `tomllib`) | 3.13 | nothing. On an older image run `sudo dnf install -y python3.11` and call helpers with `python3.11` |
| AWS CLI v2, Git, Node.js 18+ | present | nothing |
| Kiro CLI | present (`/usr/local/bin/kiro-cli`) | `kiro-cli whoami`; if not logged in, `kiro-cli login --use-device-flow` (device code; CloudShell cannot receive a browser redirect) |
| D2 (initial diagram render) | absent | `V=0.9.0; A=$( [ "$(uname -m)" = aarch64 ] && echo arm64 \|\| echo amd64 ); curl -fsSLO https://github.com/terrastruct/d2/releases/download/v$V/d2-v$V-linux-$A.tar.gz && tar xzf d2-v$V-linux-$A.tar.gz && mkdir -p ~/.local/bin && cp d2-v$V/bin/d2 ~/.local/bin/ && rm -rf d2-v$V d2-v$V-linux-$A.tar.gz && d2 --version` (the archive extracts to `d2-v$V/`, without the platform suffix) |
| Chromium + fonts (PNG export, visual QA) | browser absent, no CJK fonts | optional. The agent installs the shared libraries and Noto CJK fonts with `dnf` when it needs them; this changes only the CloudShell home/image, not AWS. Several hundred MB of the 1 GB home. If that is not acceptable, deliver HTML/SVG and record the missing PNG/visual QA as a limitation |

## 3. Credentials: a profile, never exported keys

CloudShell supplies credentials through the **container credential provider** and refreshes them
automatically. Two profile shapes work; both keep key values out of files and prompts:

- **Dedicated assessment role** (recommended; see README § Permissions for the role):
  ```bash
  ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
  aws configure set role_arn arn:aws:iam::$ACCOUNT:role/ModernizationAssessmentRole --profile assessment
  aws configure set credential_source EcsContainer --profile assessment
  aws configure set region us-east-1 --profile assessment
  ```
  `credential_source = EcsContainer` tells the CLI to assume the role with CloudShell's own
  credentials. `source_profile` pointing at a credential-less profile fails with "The source profile
  must have credentials" (observed 2026-11-03).
- **Console role as is** (when it is already read-only): `aws configure set region us-east-1
  --profile cloudshell`. The profile has no credentials of its own, so the CLI falls through to the
  container provider.

Tell the agent which profile to use. The helpers' `--environment` mode is not suitable here:
`aws configure export-credentials --format env` copies a snapshot that CloudShell lets expire after
roughly 15 minutes, and a running assessment then fails with "credentials are still expired"
(observed 2026-10-30).

## 4. One command

```bash
cd ~/assessment
kiro-cli chat
```

In the chat: `/model` (pick the model you want), `/tools trust-all` (otherwise every shell call asks
for approval), then:

```text
/moda-skill start profile assessment
```

The entry skill verifies the account, runs a read-only discovery (regions with resources, CloudFormation
stacks, common tags, SSM-managed instances) and asks you to confirm the scope in one numbered
message with defaults. Reply `confirm` or list the changes (`1 us-east-1 only, 3 yes`). The
confirmed scope is written to `artifacts/<workload>/<run>/scope.json` and no later stage asks
again. `use defaults` skips the interview; naming region, stack and language in the prompt skips
those questions. `/moda-skill status` shows progress and `/moda-skill resume` continues after a dropped
session.

Keep the tab open: CloudShell ends the session after about 20 minutes without input. The home
directory and artifacts survive; `kiro-cli chat --resume` continues the conversation.

## 5. Getting results out

Artifacts are written under `artifacts/` in the project directory. Download with
**Actions > Download file** (one file at a time, so zip first):

```bash
RUN=$(ls -d artifacts/<name>/*/ | tail -1)
zip -qr ~/assessment-$(basename $RUN).zip "$RUN"
# Download file path: /home/cloudshell-user/assessment-<timestamp>.zip
```

Review the archive for account details before sharing it outside the team.

## Limits

- Home storage is 1 GB and is purged after 120 days without use; keep artifacts elsewhere if you need them longer.
- Outbound network is required for the AWS Knowledge MCP server, pinned companion skills, D2 and Kiro; some organizations restrict CloudShell egress.
- The browser's native file picker cannot be driven by page automation; uploading the zip is a manual click.

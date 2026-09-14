# Runbook: IAM E2E with CredentialsOverrider

Manual end-to-end testing of the IAM service's `CredentialsOverrider` support
against real clouds, for all three credential types:

- **`SESSION`** — static credentials (access key / bearer token) you already hold.
- **`ASSUME_ROLE`** — assume a target role / impersonate a target service account.
- **`ASSUME_ROLE_WEB_IDENTITY`** — federate a real OIDC token into the cloud.

Each run drives the full cloud-agnostic IAM identity lifecycle (create → get →
attach/list/get/remove policy → delete) through `IamClient`, and cross-verifies
every step with the raw provider SDK. The `whoami` pre-step proves the override
actually took effect (it prints the *effective* principal, not just that a
client constructed).

> This runbook is **templatized** — replace every `<PLACEHOLDER>` with your own
> values. Nothing here is tied to a specific account, project, role, or service
> account.

---

## Placeholders

| Placeholder | Meaning |
|---|---|
| `<AWS_ACCOUNT_ID>` | Your AWS account ID (the `--tenant-id` for AWS runs). |
| `<AWS_REGION>` | AWS region, e.g. `us-east-1`. |
| `<GCP_PROJECT_ID>` | Your GCP project ID (the `--tenant-id` for GCP runs). |
| `<GCP_REGION>` | GCP region, e.g. `us-central1`. |
| `<TARGET_ROLE_ARN>` | AWS role ARN to assume (`ASSUME_ROLE` / `ASSUME_ROLE_WEB_IDENTITY`). |
| `<TARGET_SA_EMAIL>` | GCP service account email to impersonate (`ASSUME_ROLE`). |
| `<OIDC_SA_EMAIL>` | GCP service account used as the OIDC **identity provider** (web-identity). |
| `<OIDC_SA_UNIQUE_ID>` | Unique ID of `<OIDC_SA_EMAIL>` — becomes the token `aud` and the AWS trust condition. |
| `<TEST_ROLE_NAME>` | Name of the AWS role that trusts the OIDC provider (web-identity). |

---

## Common prerequisites

1. **Repo + harness installed.** From the repo root, on the branch under test:
   ```bash
   uv sync
   python3 -m pip install -e tests/e2e     # installs the `python-multicloud-sdk-e2e` CLI
   python-multicloud-sdk-e2e iam --help    # confirm the --creds-type flags are present
   ```
2. **Cloud CLIs authenticated:**
   ```bash
   aws sts get-caller-identity                       # AWS
   gcloud auth login && gcloud auth application-default login   # GCP
   ```
3. **Base permissions.** Whatever principal the override resolves to must be
   able to run the IAM lifecycle. On AWS that means these actions (attach to the
   assumed role for `ASSUME_ROLE*`, or to your user for `SESSION`):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": [
         "iam:CreateRole", "iam:GetRole", "iam:DeleteRole",
         "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
         "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy",
         "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"
       ],
       "Resource": "*"
     }]
   }
   ```

   > `iam:UpdateRole` + `iam:UpdateAssumeRolePolicy` are required by the
   > **idempotent re-create** step — it re-creates with a changed description,
   > driving the provider's update path. Omitting them fails that one step with
   > `AccessDenied` even when everything else passes.

   On GCP the impersonated / session principal needs Service Account Admin +
   Project IAM Admin equivalents (`iam.serviceAccounts.*`,
   `resourcemanager.projects.{get,set}IamPolicy`).

4. **Secrets come from the environment, never the command line.** The harness
   reads session secrets from env vars and web-identity tokens from a file at
   call time.

---

## Credential type 1 — `SESSION`

Static credentials you already hold. The harness reads them from the
environment (provider-specific, so an AWS session can't leak into a GCP run).

### AWS — SESSION

Source env vars: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and (if temporary)
`AWS_SESSION_TOKEN`.

```bash
# Export the credentials you want the SDK to use as a static session.
# e.g. from an assumed role or SSO export:
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...          # required if the base creds are temporary

python-multicloud-sdk-e2e iam \
  --provider aws \
  --tenant-id <AWS_ACCOUNT_ID> \
  --region <AWS_REGION> \
  --creds-type session \
  --non-interactive
```

### GCP — SESSION

Source env var: `MULTICLOUD_E2E_BEARER_TOKEN` (an OAuth2 access token). The GCP
bearer token *is* the credential and is non-refreshable, so mint a fresh one.

```bash
export MULTICLOUD_E2E_BEARER_TOKEN="$(gcloud auth print-access-token)"

python-multicloud-sdk-e2e iam \
  --provider gcp \
  --tenant-id <GCP_PROJECT_ID> \
  --region <GCP_REGION> \
  --creds-type session \
  --non-interactive
```

**Pass:** whoami prints the caller principal (AWS caller ARN / validated GCP
OAuth2 token), lifecycle = 9/9.

---

## Credential type 2 — `ASSUME_ROLE`

Assume an AWS role, or impersonate a GCP service account. No secrets on the
command line — the override carries the target and the SDK resolves it from your
ambient credentials.

### AWS — ASSUME_ROLE

Prereq: your base identity can `sts:AssumeRole` on `<TARGET_ROLE_ARN>`, and that
role has the IAM lifecycle permissions from the prerequisites.

```bash
python-multicloud-sdk-e2e iam \
  --provider aws \
  --tenant-id <AWS_ACCOUNT_ID> \
  --region <AWS_REGION> \
  --creds-type assume-role \
  --assume-role-target <TARGET_ROLE_ARN> \
  --session-name multicloud-e2e \
  --duration-seconds 3600 \
  --non-interactive
```

### GCP — ASSUME_ROLE (impersonation)

Prereq: your base identity holds `roles/iam.serviceAccountTokenCreator` on
`<TARGET_SA_EMAIL>`, and that SA can manage IAM in the project.

```bash
# Grant impersonation if needed:
gcloud iam service-accounts add-iam-policy-binding <TARGET_SA_EMAIL> \
  --project=<GCP_PROJECT_ID> \
  --member="user:<you>@example.com" \
  --role="roles/iam.serviceAccountTokenCreator"

python-multicloud-sdk-e2e iam \
  --provider gcp \
  --tenant-id <GCP_PROJECT_ID> \
  --region <GCP_REGION> \
  --creds-type assume-role \
  --assume-role-target <TARGET_SA_EMAIL> \
  --duration-seconds 3600 \
  --non-interactive
```

**Pass:** whoami prints the assumed-role ARN (AWS) / impersonated SA email
(GCP), lifecycle = 9/9.

---

## Credential type 3 — `ASSUME_ROLE_WEB_IDENTITY`

Federate a **real OIDC token** into the cloud. You cannot hand-craft the JWT —
the cloud validates its signature against a registered IdP's JWKS. Below uses a
**GCP service account as the OIDC identity provider for AWS**, so the whole flow
runs locally with `gcloud` + `aws` (no CI runner or pod required).

### AWS — ASSUME_ROLE_WEB_IDENTITY (fully supported)

How it resolves: `get_session` builds an
`AssumeRoleWithWebIdentityCredentialFetcher` and exchanges the OIDC token for STS
creds via `sts:AssumeRoleWithWebIdentity`. The token-exchange STS client is
**anonymous**, so the OIDC token is the sole input.

**One-time AWS setup** — register the OIDC provider and a role that trusts it:

```bash
# 1. Register the IdP (Google) as an OIDC provider, trusting the token's aud
aws iam create-open-id-connect-provider \
  --url https://accounts.google.com \
  --client-id-list <OIDC_SA_UNIQUE_ID>

# 2. Trust policy gating on aud == the OIDC SA's unique id
cat > web-identity-trust.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::<AWS_ACCOUNT_ID>:oidc-provider/accounts.google.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": { "accounts.google.com:aud": "<OIDC_SA_UNIQUE_ID>" }
    }
  }]
}
JSON
aws iam create-role \
  --role-name <TEST_ROLE_NAME> \
  --assume-role-policy-document file://web-identity-trust.json

# 3. Attach the IAM lifecycle permissions (see prerequisites JSON)
aws iam put-role-policy \
  --role-name <TEST_ROLE_NAME> \
  --policy-name iam-e2e-lifecycle \
  --policy-document file://iam-e2e-lifecycle.json
```

**One-time GCP setup** — you must be able to mint the OIDC SA's token:

```bash
# Find the OIDC SA's unique id (this value is the aud / trust condition above)
gcloud iam service-accounts describe <OIDC_SA_EMAIL> \
  --project=<GCP_PROJECT_ID> --format='value(email, uniqueId)'

# Grant yourself impersonation on the OIDC SA
gcloud iam service-accounts add-iam-policy-binding <OIDC_SA_EMAIL> \
  --project=<GCP_PROJECT_ID> \
  --member="user:<you>@example.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

**Run:**

```bash
# 1. Mint the Google OIDC token — aud MUST equal <OIDC_SA_UNIQUE_ID>
gcloud auth print-identity-token \
  --impersonate-service-account=<OIDC_SA_EMAIL> \
  --audiences="<OIDC_SA_UNIQUE_ID>" \
  --include-email \
  > /tmp/oidc-token.txt

# (optional) verify the aud claim before running
cut -d. -f2 /tmp/oidc-token.txt | base64 -d 2>/dev/null | python3 -m json.tool

# 2. Run the AWS web-identity lifecycle
python-multicloud-sdk-e2e iam \
  --provider aws \
  --tenant-id <AWS_ACCOUNT_ID> \
  --region <AWS_REGION> \
  --creds-type web-identity \
  --assume-role-target arn:aws:iam::<AWS_ACCOUNT_ID>:role/<TEST_ROLE_NAME> \
  --web-identity-token-file /tmp/oidc-token.txt \
  --non-interactive
```

**Pass:** whoami prints `arn:aws:sts::<AWS_ACCOUNT_ID>:assumed-role/<TEST_ROLE_NAME>/...`
(the federated identity, not your ambient one), lifecycle = 9/9.

### GCP — ASSUME_ROLE_WEB_IDENTITY (documented no-op → ADC)

GCP does **not** support this override type: the shared
`gcp_common.get_credentials` returns `None` (emitting a loud `_logger.warning`)
and the provider falls through to Application Default Credentials. The goal here
is to verify the fall-through is clean, not that federation happened.

```bash
gcloud auth application-default login          # ensure ADC is present
echo "unused-on-gcp" > /tmp/dummy-token.txt    # content is never read on GCP

python-multicloud-sdk-e2e iam \
  --provider gcp \
  --tenant-id <GCP_PROJECT_ID> \
  --region <GCP_REGION> \
  --creds-type web-identity \
  --assume-role-target <TARGET_SA_EMAIL> \
  --web-identity-token-file /tmp/dummy-token.txt \
  --non-interactive
```

**Pass:** whoami prints your ADC principal followed by
`(override no-op: WEB_IDENTITY unsupported on GCP)`, lifecycle = 9/9 under ADC.

---

## Interpreting results

| Signal | Meaning |
|---|---|
| `RESULT: 9/9 steps passed` | Full lifecycle succeeded under the overridden identity. |
| whoami shows the target principal | The override took effect (not ambient creds). |
| `AccessDenied` on a single step | Permission gap on the resolved principal — see the prerequisites permission set (esp. `iam:UpdateRole`). |
| GCP whoami shows `(override no-op...)` | Expected for GCP web-identity; ran under ADC. |

## Common gotchas

- **Token/credential expiry:** temporary AWS creds and GCP bearer/OIDC tokens
  are short-lived (~1h). A full run takes ~1–3 min (verifier polling for
  eventual consistency). Re-mint before re-running if near expiry.
- **Secrets on the CLI:** never pass keys/tokens as flags. SESSION reads env
  vars; web-identity reads a token file at call time.
- **AWS web-identity trust:** gate the trust policy on `aud`. Any token minted
  for the OIDC SA with the matching `aud` is accepted.
- **Idempotent re-create step:** if only step 2 fails with `AccessDenied`, the
  resolved principal is missing `iam:UpdateRole` / `iam:UpdateAssumeRolePolicy`.

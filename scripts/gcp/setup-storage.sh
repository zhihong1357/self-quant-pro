#!/usr/bin/env bash
# One-time Google Cloud setup for G1 (storage only, decision 0015).
# Run it in Cloud Shell, as the project owner:
#
#   curl -fsSLo sqp-setup.sh https://raw.githubusercontent.com/zhihong1357/self-quant-pro/main/scripts/gcp/setup-storage.sh && bash sqp-setup.sh PROJECT_ID
#
# Download first, run only on success: with bash <(curl ...), a 404 runs an
# empty script silently and the owner is left at the shell prompt.
#
# It creates a private bucket, a Workload Identity pool/provider that only
# admits scheduled or manual workflow runs on main of this repository, bucket
# roles that allow create + read but never delete (so never overwrite), and an
# alert-only budget. No service account, no key file. Safe to re-run: existing
# resources are kept and the provider condition and bucket roles are re-applied.
# Owner-facing messages are in Chinese (AGENTS.md section 3).
set -euo pipefail

# GitHub's numeric IDs are never reused, unlike names, so a deleted or renamed
# repository cannot be squatted (google-github-actions/auth security notes).
readonly REPO_ID="1468841217"  # zhihong1357/self-quant-pro
readonly OWNER_ID="265740839"  # zhihong1357
readonly REGION="us-central1"
readonly POOL="github"
readonly PROVIDER="self-quant-pro"
readonly BUDGET_NAME="sqp-monthly"
readonly ISSUER="https://token.actions.githubusercontent.com"
# Every claim used in the condition must be mapped first.
readonly MAPPING="google.subject=assertion.sub,attribute.repository_id=assertion.repository_id,attribute.repository_owner_id=assertion.repository_owner_id,attribute.ref=assertion.ref,attribute.event_name=assertion.event_name"
# pull_request and pull_request_target runs are rejected by the event_name test.
readonly CONDITION="assertion.repository_owner_id == '${OWNER_ID}' && assertion.repository_id == '${REPO_ID}' && assertion.ref == 'refs/heads/main' && (assertion.event_name == 'schedule' || assertion.event_name == 'workflow_dispatch')"

say() { printf '\n== %s\n' "$*"; }
fail() {
  printf '\n出错：%s\n' "$*" >&2
  exit 1
}

PROJECT_ID="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
[[ -n "$PROJECT_ID" ]] || fail "没有项目 ID。用法：在命令最后加上你的项目 ID。"
[[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] ||
  fail "「${PROJECT_ID}」不像项目 ID（小写字母、数字、连字符，6–30 个字符）。到 console.cloud.google.com 顶部的项目选择框里复制。"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')" ||
  fail "找不到项目 ${PROJECT_ID}，或者当前账号没有权限。检查 ID 有没有抄错、登录的是不是 AI Pro 那个账号。"
readonly PROJECT_NUMBER
readonly BUCKET="${PROJECT_ID}-sqp-data"

printf '\n将在谷歌云项目 %s（编号 %s）里创建：存储桶、GitHub 身份联合、预算提醒。\n' "$PROJECT_ID" "$PROJECT_NUMBER"
read -r -p '确认无误请输入 yes 回车：' answer
[[ "$answer" == "yes" ]] || fail "已取消，什么都没改。"

say "启用需要的接口（启用本身不收费）"
gcloud services enable iam.googleapis.com sts.googleapis.com storage.googleapis.com \
  cloudbilling.googleapis.com billingbudgets.googleapis.com cloudresourcemanager.googleapis.com \
  --project="$PROJECT_ID"

say "检查结算账号"
BILLING_ENABLED="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)')"
[[ "${BILLING_ENABLED,,}" == "true" ]] ||
  fail "项目还没有绑定结算账号。先做 docs/gcp-setup.md 的第 1、2 步，再重新运行。"
BILLING_ACCOUNT="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName)')"
readonly BILLING_ACCOUNT="${BILLING_ACCOUNT#billingAccounts/}"
echo "已绑定。"

say "存储桶 gs://${BUCKET}"
if gcloud storage buckets describe "gs://${BUCKET}" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets update "gs://${BUCKET}" --project="$PROJECT_ID" \
    --uniform-bucket-level-access --public-access-prevention >/dev/null
  echo "已存在：保留，并重新确认统一权限、禁止公开访问。"
else
  gcloud storage buckets create "gs://${BUCKET}" --project="$PROJECT_ID" \
    --location="$REGION" --default-storage-class=STANDARD \
    --uniform-bucket-level-access --public-access-prevention
fi

say "身份池 ${POOL}"
POOL_STATE="$(gcloud iam workload-identity-pools describe "$POOL" --project="$PROJECT_ID" \
  --location=global --format='value(state)' 2>/dev/null || true)"
case "$POOL_STATE" in
  ACTIVE) echo "已存在，保留。" ;;
  DELETED)
    gcloud iam workload-identity-pools undelete "$POOL" --project="$PROJECT_ID" --location=global
    ;;
  *)
    gcloud iam workload-identity-pools create "$POOL" --project="$PROJECT_ID" --location=global \
      --display-name="GitHub Actions"
    ;;
esac

say "身份提供方 ${PROVIDER}：只认本仓库 main 分支上定时或手动触发的工作流"
PROVIDER_STATE="$(gcloud iam workload-identity-pools providers describe "$PROVIDER" \
  --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL" \
  --format='value(state)' 2>/dev/null || true)"
if [[ "$PROVIDER_STATE" == "DELETED" ]]; then
  gcloud iam workload-identity-pools providers undelete "$PROVIDER" \
    --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL"
  PROVIDER_STATE="ACTIVE"
fi
if [[ "$PROVIDER_STATE" == "ACTIVE" ]]; then
  gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" \
    --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL" \
    --issuer-uri="$ISSUER" --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION"
else
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL" \
    --display-name="self-quant-pro main" \
    --issuer-uri="$ISSUER" --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION"
fi

say "存储桶权限：这个身份只能新建对象和读对象，不能删除、不能覆盖"
readonly MEMBER="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository_id/${REPO_ID}"
for role in roles/storage.objectCreator roles/storage.objectViewer; do
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="$MEMBER" --role="$role" >/dev/null
  echo "已授予 ${role}"
done

say "预算提醒 ${BUDGET_NAME}"
budget_todo=""
# Capture first: with pipefail, grep -q exiting early could fail the pipeline.
existing_budgets="$(gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" \
  --billing-project="$PROJECT_ID" --format='value(displayName)' 2>/dev/null || true)"
if grep -qxF "$BUDGET_NAME" <<<"$existing_budgets"; then
  echo "已存在，保留。"
elif gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" --billing-project="$PROJECT_ID" \
  --display-name="$BUDGET_NAME" --budget-amount=10USD --calendar-period=month \
  --filter-projects="projects/${PROJECT_ID}" --credit-types-treatment=exclude-all-credits \
  --threshold-rule=percent=0.1 --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 >/dev/null; then
  echo "已建好：每月 10 美元，按原价计算，花到 1、5、9、10 美元时发邮件。"
else
  budget_todo="yes"
  echo "没建成（常见原因：结算账号的币种不是美元）。其他步骤不受影响，稍后按 docs/gcp-setup.md 第 4 步在网页上建。"
fi

say "完成。把下面三行原样发给智能体（都不是密钥）："
printf 'GCP_PROJECT_ID=%s\n' "$PROJECT_ID"
printf 'GCP_WIF_PROVIDER=projects/%s/locations/global/workloadIdentityPools/%s/providers/%s\n' \
  "$PROJECT_NUMBER" "$POOL" "$PROVIDER"
printf 'SQP_BUCKET=%s\n' "$BUCKET"
if [[ -n "$budget_todo" ]]; then
  printf '\n另外：预算提醒没建成，请按 docs/gcp-setup.md 第 4 步在网页上建。\n'
fi
printf '\n身份和权限最多要 5 分钟才生效。\n'

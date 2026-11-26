#!/usr/bin/env bash
# One-time setup of the daily Binance recorder in Tokyo (decision 0027).
# Run it in Cloud Shell, as the project owner:
#
#   curl -fsSLo sqp-recorder.sh https://raw.githubusercontent.com/zhihong1357/self-quant-pro/main/scripts/gcp/setup-recorder.sh && bash sqp-recorder.sh PROJECT_ID
#
# It creates a service account the job runs as, with create + read on the bucket (never
# delete, so never overwrite); a Cloud Run job in asia-northeast1 whose image is the
# stock python:3.12-slim and whose command fetches scripts/recorder/bootstrap.py from
# main; and a Cloud Scheduler job that starts it every day at 00:17 UTC. Then it runs a
# two-minute probe (can Tokyo reach Binance?) and, if that passes, starts the first real
# run in the background. No key file. Safe to re-run: existing resources are updated.
#
#   bash sqp-recorder.sh PROJECT_ID logs     # print the latest run's log
#
# Owner-facing messages are in Chinese (AGENTS.md section 3).
set -euo pipefail

# Tokyo: Binance's main API refuses addresses in the United States (decision 0027).
readonly REGION="asia-northeast1"
readonly JOB="binance-recorder"
readonly SCHEDULER_JOB="binance-recorder-daily"
readonly SCHEDULE="17 0 * * *" # UTC: after the 00:00 settlement, off the round minute
readonly IMAGE="docker.io/library/python:3.12-slim"
readonly BOOTSTRAP="https://raw.githubusercontent.com/zhihong1357/self-quant-pro/main/scripts/recorder/bootstrap.py"
# gcloud splits --args on commas, so the code must not contain one.
readonly CODE="import urllib.request as u; exec(u.urlopen('${BOOTSTRAP}').read())"
readonly SA_NAME="binance-recorder"

say() { printf '\n== %s\n' "$*"; }
fail() {
  printf '\n出错：%s\n' "$*" >&2
  exit 1
}

PROJECT_ID="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
MODE="${2:-setup}"
[[ -n "$PROJECT_ID" ]] || fail "没有项目 ID。用法：在命令最后加上你的项目 ID。"
[[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] ||
  fail "「${PROJECT_ID}」不像项目 ID（小写字母、数字、连字符，6–30 个字符）。到 console.cloud.google.com 顶部的项目选择框里复制。"
readonly BUCKET="${PROJECT_ID}-sqp-data"
readonly SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
readonly LOG_FILTER="resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB}\""

show_logs() {
  # Newest first from the API; reversed so the log reads top to bottom.
  gcloud logging read "$LOG_FILTER" --project="$PROJECT_ID" --freshness="${1:-2d}" \
    --limit=40 --format='value(textPayload)' | tac
}

if [[ "$MODE" == "logs" ]]; then
  say "最近一次运行的日志（最后 40 行）"
  show_logs 2d
  exit 0
fi
[[ "$MODE" == "setup" ]] || fail "第二个参数只能不写，或者写 logs。"

gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' >/dev/null ||
  fail "找不到项目 ${PROJECT_ID}，或者当前账号没有权限。检查 ID 有没有抄错、登录的是不是 AI Pro 那个账号。"

printf '\n将在谷歌云项目 %s 的东京区（%s）建一个每天运行一次的 Binance 录制任务。\n' "$PROJECT_ID" "$REGION"
printf '它只读 Binance 的公开行情，不用 Binance 账号，也不能下单。\n'
read -r -p '确认无误请输入 yes 回车：' answer
[[ "$answer" == "yes" ]] || fail "已取消，什么都没改。"

say "启用 Cloud Run 和 Cloud Scheduler（启用本身不收费）"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com --project="$PROJECT_ID"

say "检查存储桶 gs://${BUCKET}"
gcloud storage buckets describe "gs://${BUCKET}" --project="$PROJECT_ID" >/dev/null 2>&1 ||
  fail "找不到存储桶。它是 G1 的设置建的（docs/gcp-setup.md），先确认项目 ID 没抄错。"
echo "在。"

say "录制任务的身份 ${SA}"
if gcloud iam service-accounts describe "$SA" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "已存在，保留。"
else
  gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT_ID" \
    --display-name="Binance recorder (decision 0027)"
  sleep 15 # a new account takes a moment before it can be granted anything
fi

say "存储桶权限：这个身份只能新建对象和读对象，不能删除、不能覆盖"
for role in roles/storage.objectCreator roles/storage.objectViewer; do
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="serviceAccount:${SA}" --role="$role" >/dev/null
  echo "已授予 ${role}"
done

say "Cloud Run 任务 ${JOB}（东京区）"
# No retries: a 418 (banned) must stop at once, and anything else is picked up by the
# next day's run, which continues from where the last one stopped. Cloud Run imports the
# image when the job is deployed, so the daily runs do not pull from Docker Hub.
# 2 GiB: the installed environment (~200 MB) lives in memory too, and the first run
# went out of memory at 1 GiB before lines were compressed as they arrive.
gcloud run jobs deploy "$JOB" --project="$PROJECT_ID" --region="$REGION" --image="$IMAGE" \
  --service-account="$SA" --command=python --args="-c,${CODE}" \
  --tasks=1 --cpu=1 --memory=2Gi --task-timeout=3600s --max-retries=0 --quiet

say "允许定时器启动这个任务"
gcloud run jobs add-iam-policy-binding "$JOB" --project="$PROJECT_ID" --region="$REGION" \
  --member="serviceAccount:${SA}" --role=roles/run.invoker >/dev/null
echo "好了。"

say "定时器 ${SCHEDULER_JOB}：每天 UTC 00:17"
readonly URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run"
readonly SCOPE="https://www.googleapis.com/auth/cloud-platform"
if gcloud scheduler jobs describe "$SCHEDULER_JOB" --project="$PROJECT_ID" --location="$REGION" >/dev/null 2>&1; then
  verb=update
else
  verb=create
fi
gcloud scheduler jobs "$verb" http "$SCHEDULER_JOB" --project="$PROJECT_ID" --location="$REGION" \
  --schedule="$SCHEDULE" --time-zone="Etc/UTC" --uri="$URI" --http-method=POST \
  --oauth-service-account-email="$SA" --oauth-token-scope="$SCOPE" >/dev/null
echo "好了。"

say "试一下东京能不能连上 Binance（约 2 分钟，只打印状态码）"
if gcloud run jobs execute "$JOB" --project="$PROJECT_ID" --region="$REGION" \
  --update-env-vars=SQP_PROBE=1 --wait; then
  probe_ok=yes
else
  probe_ok=""
fi
sleep 20 # logs arrive a little after the run ends
show_logs 30m

if [[ -z "$probe_ok" ]]; then
  fail "试连没有成功。把上面的日志截图发给智能体。定时器已经建好，问题解决前每天都会失败一次，不会写入任何数据。"
fi

say "开始第一次正式录制（要补最近 29 天，约 30 分钟，在后台跑）"
gcloud run jobs execute "$JOB" --project="$PROJECT_ID" --region="$REGION" --async >/dev/null
echo "已经开始。你现在可以关掉 Cloud Shell。"
printf '\n半小时后想看结果，在 Cloud Shell 里运行：\n  bash sqp-recorder.sh %s logs\n' "$PROJECT_ID"
printf '或者打开 console.cloud.google.com/run/jobs → 点 %s：绿色对勾就是成功。\n' "$JOB"
printf '\n完成。以后每天 UTC 00:17 自动运行。\n'

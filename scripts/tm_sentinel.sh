#!/bin/bash
# tm-server 简易 sentinel — 周期性写 prometheus textfile + 异常 syslog 告警
#
# 部署要求：
#   1. eva 上 node-exporter container 启用 --collector.textfile + bind mount 一个 host 目录
#      （示例 docker run 加 -v /var/lib/node-exporter/textfile:/textfile + --collector.textfile.directory=/textfile）
#   2. /etc/default/tm-sentinel 文件（mode 600）含：
#        TM_API_KEY=sk-...
#        TM_ENDPOINT=https://your-tm-server.example.com   # 可选
#        TM_PROBE_PROFILES="gemini-3072 openai-small-1024"   # 可选
#   3. cron: */5 * * * * /usr/local/bin/tm-sentinel.sh
#
# 设计：见 docs/plan/observability-design.md §Layer 1
#
# 安全设计（v0.11.0-pre 审计修复）：
#   - API key 通过 curl -K config 文件传，**不**走 argv → 不在 ps 暴露
#   - /tmp 写入用 mktemp，trap 清理，防 symlink TOCTOU
#   - 所有数值字段加 sanitization 防止 set -u 被 curl 失败 string 触发

set -uo pipefail

# 加载 secret env（若文件存在）
[ -f /etc/default/tm-sentinel ] && . /etc/default/tm-sentinel

SRV="${TM_ENDPOINT:-https://your-tm-server.example.com}"
if [ -z "${TM_API_KEY:-}" ]; then
  logger -t tm-sentinel -p user.err "TM_API_KEY 未设置（应在 /etc/default/tm-sentinel 或 env 注入）"
  exit 1
fi
OUT="${TM_TEXTFILE_OUT:-/var/lib/node-exporter/textfile/tm.prom}"
PROBE_PROFILES="${TM_PROBE_PROFILES:-gemini-3072 openai-small-1024}"

# 临时文件全部用 mktemp + trap 清理（防 symlink TOCTOU + 自清理）
TMP=$(mktemp -t tm-prom.XXXXXX)
HEALTH_TMP=$(mktemp -t tm-health.XXXXXX)
CURL_CFG=$(mktemp -t tm-curl-cfg.XXXXXX)
trap 'rm -f "$TMP" "$HEALTH_TMP" "$CURL_CFG"' EXIT INT TERM

# curl config 文件（mode 600 by mktemp）传 header，避免 API key 进 argv
chmod 600 "$CURL_CFG"
printf 'header = "X-API-KEY: %s"\n' "$TM_API_KEY" > "$CURL_CFG"

write() { echo "$1" >> "$TMP"; }
# 把可能含 non-numeric 的 curl 解析结果安全转 int（默认 0）
num() { case "$1" in ''|*[!0-9]*) echo 0;; *) echo "$1";; esac }

# 文件头
: > "$TMP"
write "# tm-server sentinel — generated $(date -Iseconds)"

# 1) /health — 不带 auth (公开 endpoint)
HEALTH=$(curl -sS -m 8 -o "$HEALTH_TMP" -w '%{http_code}' "$SRV/health" 2>/dev/null || echo "000")
if [ "$HEALTH" = "200" ]; then UP=1; else UP=0; fi

write "# HELP tm_health_up service /health endpoint reachable + 200 OK"
write "# TYPE tm_health_up gauge"
write "tm_health_up $UP"

AI=0
WORKER=0
UPTIME=0
if [ "$UP" -eq 1 ]; then
  AI=$(python3 -c "import json; d=json.load(open('$HEALTH_TMP')); print(1 if d.get('accepting_ingest') else 0)" 2>/dev/null || echo 0)
  WORKER=$(python3 -c "import json; d=json.load(open('$HEALTH_TMP')); print(1 if d.get('worker_running') else 0)" 2>/dev/null || echo 0)
  UPTIME=$(python3 -c "import json; d=json.load(open('$HEALTH_TMP')); print(int(d.get('uptime_seconds', 0)))" 2>/dev/null || echo 0)
fi
write "# TYPE tm_accepting_ingest gauge"
write "tm_accepting_ingest $(num "$AI")"
write "# TYPE tm_worker_running gauge"
write "tm_worker_running $(num "$WORKER")"
write "# TYPE tm_uptime_seconds gauge"
write "tm_uptime_seconds $(num "$UPTIME")"

# 2) /admin/system-health (需 auth → 用 curl -K config)
SYS=$(curl -sS -m 8 -K "$CURL_CFG" "$SRV/admin/system-health" 2>/dev/null || echo '{}')
PROFILE_CNT=$(echo "$SYS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('profiles',{}).get('embeddings_count',0))" 2>/dev/null || echo 0)
RERANKER_CNT=$(echo "$SYS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('profiles',{}).get('rerankers_count',0))" 2>/dev/null || echo 0)
write "# TYPE tm_profiles_embeddings_count gauge"
write "tm_profiles_embeddings_count $(num "$PROFILE_CNT")"
write "# TYPE tm_profiles_rerankers_count gauge"
write "tm_profiles_rerankers_count $(num "$RERANKER_CNT")"

# 3) per-profile probe (latency + ok + dim)
write "# TYPE tm_profile_probe_ok gauge"
write "# TYPE tm_profile_probe_latency_ms gauge"
write "# TYPE tm_profile_dim gauge"
for PROF in $PROBE_PROFILES; do
  R=$(curl -sS -m 15 -X POST -K "$CURL_CFG" "$SRV/admin/probe-embedding?profile=$PROF" 2>/dev/null || echo '{}')
  PROBE_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(1 if d.get('ok') else 0)" 2>/dev/null || echo 0)
  LAT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('latency_ms', 0))" 2>/dev/null || echo 0)
  DIM=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('dim', 0))" 2>/dev/null || echo 0)
  write "tm_profile_probe_ok{profile=\"$PROF\"} $(num "$PROBE_OK")"
  write "tm_profile_probe_latency_ms{profile=\"$PROF\"} $(num "$LAT")"
  write "tm_profile_dim{profile=\"$PROF\"} $(num "$DIM")"
done

# 4) queue health
# /jobs 返回 dict {jobs, stats, worker_running}，**不是** list（design-reviewer B3 发现）
# 优先用 stats.pending / stats.failed（避开 jobs[] 可能被 limit=50 截断）
J=$(curl -sS -m 8 -K "$CURL_CFG" "$SRV/jobs?status=pending" 2>/dev/null || echo '{}')
PENDING=$(echo "$J" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if isinstance(d, dict):
    print(d.get('stats', {}).get('pending', len(d.get('jobs', []))))
elif isinstance(d, list):
    print(len(d))
else:
    print(0)" 2>/dev/null || echo 0)

JF=$(curl -sS -m 8 -K "$CURL_CFG" "$SRV/jobs?status=failed" 2>/dev/null || echo '{}')
FAILED=$(echo "$JF" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if isinstance(d, dict):
    print(d.get('stats', {}).get('failed', len(d.get('jobs', []))))
elif isinstance(d, list):
    print(len(d))
else:
    print(0)" 2>/dev/null || echo 0)
PENDING=$(num "$PENDING")
FAILED=$(num "$FAILED")
write "# TYPE tm_queue_pending gauge"
write "tm_queue_pending $PENDING"
write "# TYPE tm_queue_failed gauge"
write "tm_queue_failed $FAILED"

# atomic rename to final path（也防止 textfile collector 读到半写状态）
mv "$TMP" "$OUT"
# 清掉 trap 释放，避免后续误删（OUT 已不在 trap path 内）
TMP=""

# 异常 → syslog
[ "$UP" -ne 1 ] && logger -t tm-sentinel -p user.err "tm-server /health DOWN (http=$HEALTH)"
[ "$UP" -eq 1 ] && [ "$AI" = "0" ] && logger -t tm-sentinel -p user.warn "tm-server accepting_ingest=false"
[ "$UP" -eq 1 ] && [ "$WORKER" = "0" ] && logger -t tm-sentinel -p user.warn "tm-server worker_running=false"
[ "$PENDING" -gt 50 ] && logger -t tm-sentinel -p user.warn "tm-server queue pending>50 ($PENDING)"
[ "$FAILED" -gt 5 ] && logger -t tm-sentinel -p user.warn "tm-server queue failed>5 ($FAILED)"

exit 0

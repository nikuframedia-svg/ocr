#!/usr/bin/env bash
# Pull vllm image with progress, log to /tmp/vllm-pull.log
set -e
LOGFILE=/tmp/vllm-pull.log
echo "starting pull at $(date)" > "$LOGFILE"
docker pull vllm/vllm-openai:latest >> "$LOGFILE" 2>&1
echo "done at $(date)" >> "$LOGFILE"

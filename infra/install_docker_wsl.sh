#!/usr/bin/env bash
# Install Docker engine + nvidia-container-toolkit inside WSL2 Ubuntu.
# Idempotent: safe to re-run.
#
# Run from Windows PowerShell:
#   wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/User/ocr/infra/install_docker_wsl.sh
#
# Or from inside WSL:
#   bash infra/install_docker_wsl.sh

set -euo pipefail

log() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

log "Probe environment"
echo "  PID 1: $(ps -p 1 -o comm=)"
echo "  OS:    $(. /etc/os-release && echo "$PRETTY_NAME")"
echo "  GPU:   $(nvidia-smi -L 2>/dev/null | head -1 || echo 'NOT VISIBLE')"

# --- Docker engine -----------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
    log "Docker already installed: $(docker --version)"
else
    log "Installing Docker engine via get.docker.com"
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
fi

# --- NVIDIA container toolkit -----------------------------------------------
if dpkg -l 2>/dev/null | grep -q nvidia-container-toolkit; then
    log "nvidia-container-toolkit already installed"
else
    log "Installing nvidia-container-toolkit"
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -qq
    apt-get install -y nvidia-container-toolkit
fi

# --- Configure Docker daemon to use nvidia runtime ---------------------------
log "Configuring Docker runtime for NVIDIA"
nvidia-ctk runtime configure --runtime=docker

# --- Generate CDI spec (required by toolkit ≥1.18 for `--gpus all`) ----------
log "Generating CDI spec"
mkdir -p /etc/cdi
nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null

# --- Start Docker daemon -----------------------------------------------------
# WSL2 may or may not have systemd. Try service first, fall back to direct dockerd.
log "Starting Docker daemon"
if pidof systemd >/dev/null 2>&1; then
    systemctl enable --now docker
    systemctl status docker --no-pager | head -5
else
    if ! pgrep -f dockerd >/dev/null; then
        nohup dockerd >/var/log/dockerd.log 2>&1 &
        sleep 3
    fi
    # Confirm daemon socket
    docker info >/dev/null 2>&1 || {
        echo "Docker daemon not responding — last 30 lines of /var/log/dockerd.log:"
        tail -n 30 /var/log/dockerd.log || true
        exit 1
    }
fi

log "Verify GPU access from a container"
docker run --rm --gpus all nvidia/cuda:12.3.0-base-ubuntu22.04 nvidia-smi -L \
    || { echo "GPU NOT visible from container — toolkit/runtime issue"; exit 1; }

log "Done"
docker --version
echo "Daemon socket: $(ls -l /var/run/docker.sock 2>/dev/null || echo 'NOT FOUND')"

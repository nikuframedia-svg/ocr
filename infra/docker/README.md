# infra/docker

Phase 0 uses the official `vllm/vllm-openai:latest` image directly — no
custom build is required. This directory exists as the canonical home
for service Dockerfiles in later phases (FastAPI backend, frontend PWA,
review UI). Add Dockerfiles here when those phases come up.

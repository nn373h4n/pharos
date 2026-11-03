#!/usr/bin/env bash
# Puts a model into the volume of the local model server (profile "local-llm", ADR-0020).
#
# The server itself has no route to the internet. This one-shot job has one, and only while it
# runs: it downloads the model from the Ollama registry into the shared volume and exits. Run it
# once per model, then start (or restart) the server:
#   scripts/ollama-pull.sh qwen2.5:7b
#   docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile local-llm up -d
#
# What you download is code-like input to a model server: choose the model on purpose, write its
# exact name and tag, and read docs/DEPLOYMENT.md ("The local model") first.
set -euo pipefail
cd "$(dirname "$0")/.."

[ $# -eq 1 ] || { echo "usage: $0 <model:tag>   (for example: qwen2.5:7b)" >&2; exit 2; }
model=$1
# A model name is letters, digits and . _ : / - only; nothing that a shell or an option could use.
if ! [[ $model =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$ ]]; then
  echo "refused: '$model' is not a model name" >&2
  exit 2
fi

# A name with a host in front ("registry.example.com/team/model") would fetch from another registry,
# and ".." has no place in a name. Only the Ollama registry is used (docs/DEPLOYMENT.md).
if [[ $model == *..* ]]; then
  echo "refused: '$model' is not a model name" >&2
  exit 2
fi
if [[ $model == */* ]]; then
  first=${model%%/*}
  if [[ $first == *.* || $first == *:* ]]; then
    echo "refused: '$model' names another registry; only the Ollama registry is used" >&2
    exit 2
  fi
fi

PHAROS_OLLAMA_PULL_MODEL=$model exec docker compose -f deploy/docker-compose.yml \
  --env-file deploy/.env --profile local-llm-pull run --rm ollama-pull

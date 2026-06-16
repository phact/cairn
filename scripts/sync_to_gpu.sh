#!/usr/bin/env bash
# Sync everything the GPU box needs to run the Cairn salience experiments —
# the bits that are NOT in git (all of /.cairn is gitignored) plus the repo
# code, plus this session's transcript. Excludes the huge/regenerable stuff:
# the 118G of cloned repos, the CPU venv, old recordings, build artifacts.
#
#   ./scripts/sync_to_gpu.sh user@gpu-host [remote_dir]
#
# Then on the box: recreate the env (CUDA torch) and launch jina_finetune.py.
set -euo pipefail

ARG="${1:?usage: sync_to_gpu.sh user@host[:/remote/path]  [remote_dir]}"
SRC="/home/tato/Desktop/cairn"
# all session state: every transcript jsonl for this project + agent memory
PROJ="/home/tato/.claude/projects/-home-tato-Desktop-cairn"

# Accept either form:
#   user@host                 -> dest defaults to  user@host:cairn-ml
#   user@host:/remote/path    -> dest IS that path (full destination given)
if [[ "$ARG" == *:* ]]; then
  BASE="$ARG"                           # full host:path given, use as-is
else
  BASE="$ARG:${2:-cairn-ml}"            # host only, append remote dir
fi
BASE="${BASE%/}"                        # strip any trailing slash
REPO_DEST="$BASE/"
SESS_DEST="${BASE}-session/"

REMOTE_HOST="${BASE%%:*}"; REMOTE_PATH="${BASE#*:}"
echo ">> repo tree + corpus data + experiment scripts  ->  $REPO_DEST"
rsync -az --info=progress2 --partial \
  --exclude='.git/' \
  --exclude='**/target/' \
  --exclude='.cairn/mlenv/' \
  --exclude='.cairn/mining/' \
  --exclude='.cairn/traces/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='*.log' \
  "$SRC/" "$REPO_DEST"
# NOTE: .cairn/corpus/_repos IS included (clone SOURCES, ~1.4G) so traces can be
# re-recorded on the box — only the per-clone target/ build artifacts (the 117G)
# and .git are stripped; rebuild with cargo on the box.

echo ">> session transcripts (*.jsonl) + agent memory  ->  $SESS_DEST"
rsync -az --info=progress2 --include='*.jsonl' --include='memory/***' \
  --exclude="*" "$PROJ/" "$SESS_DEST"

cat <<EOF

done. transferred: repo code + .cairn/corpus (sans _repos, ~300M) +
.cairn/experiments (scripts + caches) + transcript + memory.

On the GPU box ($REMOTE_HOST):
  cd $REMOTE_PATH
  # recreate the env with CUDA torch (CPU mlenv was NOT copied):
  uv venv .cairn/gpuenv --python 3.13
  uv pip install --python .cairn/gpuenv torch --index-url https://download.pytorch.org/whl/cu124
  uv pip install --python .cairn/gpuenv transformers sentence-transformers numpy
  # run the live-Jina fine-tune probe:
  .cairn/gpuenv/bin/python -u .cairn/experiments/sgformer/jina_finetune.py
EOF

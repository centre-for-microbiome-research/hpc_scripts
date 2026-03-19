#!/bin/bash
# Rebuild ai_tool.sif from ai_tool.def.
# Intended to be submitted via mqsub for monthly automated rebuilds so that
# the bundled AI CLI tools (claude, codex, copilot) stay up to date.
#
# Submitted by cron via:
#   mqsub --hours 1 --threads 1 --name mqyolo_singularity_rebuild --bg \
#       --script /work/microbiome/sw/hpc_scripts/singularity/rebuild_ai_tool_sif.sh

set -euo pipefail

SINGULARITY=singularity
SDIR=/work/microbiome/sw/hpc_scripts/singularity

cd "$SDIR"

echo "$(date): starting ai_tool.sif rebuild" >&2
"$SINGULARITY" build --fakeroot ai_tool.new.sif ai_tool.def
mv ai_tool.new.sif ai_tool.sif
echo "$(date): rebuild complete" >&2

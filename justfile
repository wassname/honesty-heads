set shell := ["bash", "-cu"]

# PI/OpenAI: use the locked environment for every stage.
default:
    @just --list

sync:
    uv sync --locked --python 3.12

# PI/OpenAI: random weights and tiny data change scale, not the pipeline.
fast-dev-run:
    #!/usr/bin/env bash
    set -euo pipefail
    export BEARTYPE=1
    stamp=$(date +%Y%m%d_%H%M%S)
    uv run --locked python -m replication.run --tiny --dtype float32 --on-demand-float32 --checkpoint-vjp --method all --shuffled-seed 42 --n-select-per-class 2 --n-test-per-class 2 --n-persona-per-half 1 --n-traits 2 --random-directions 2 --max-new-tokens 4 --n-generation-per-stratum 1 --n-controls-per-polarity 1 --out "outputs/${stamp}_smoke_qwen35"
    uv run --locked python -m replication.prepare --tiny --device cpu --model Qwen/Qwen3.5-4B --revision 851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a --n-per-split 2 --max-new-tokens 4 --out "outputs/${stamp}_prepare_smoke_qwen35"
    uv run --locked python -m replication.sample --baseline-run "outputs/${stamp}_prepare_smoke_qwen35" --n-scenarios 2 --max-new-tokens 4 --out "outputs/${stamp}_sample_smoke_qwen35"

# PI/OpenAI: this uses the same pipeline on ordinary-attention Qwen3.
fast-dev-run-qwen3:
    #!/usr/bin/env bash
    set -euo pipefail
    export BEARTYPE=1
    stamp=$(date +%Y%m%d_%H%M%S)
    uv run --locked python -m replication.run --tiny --model Qwen/Qwen3-0.6B --revision c1899de289a04d12100db370d81485cdf75e47ca --dtype float32 --on-demand-float32 --checkpoint-vjp --method all --shuffled-seed 42 --n-select-per-class 2 --n-test-per-class 2 --n-persona-per-half 1 --n-traits 2 --random-directions 2 --max-new-tokens 4 --n-generation-per-stratum 1 --n-controls-per-polarity 1 --out "outputs/${stamp}_smoke_qwen3"
    uv run --locked python -m replication.prepare --tiny --device cpu --model Qwen/Qwen3-4B-Instruct-2507 --revision cdbee75f17c01a7cc42f958dc650907174af0554 --n-per-split 2 --max-new-tokens 4 --out "outputs/${stamp}_prepare_smoke_qwen3"
    uv run --locked python -m replication.sample --baseline-run "outputs/${stamp}_prepare_smoke_qwen3" --n-scenarios 2 --max-new-tokens 4 --out "outputs/${stamp}_sample_smoke_qwen3"

# PI/OpenAI: the public snapshot needs no local run directories.
results:
    uv run --locked python -m replication.public_summary

# PI/OpenAI: refresh the snapshot and appendix after local runs.
refresh-results:
    uv run --locked python -m replication.results

# PI/OpenAI: verify the published numbers without loading a model.
check-results:
    uv run --locked python -m replication.check_public_summary

# PI/OpenAI: queue real GPU runs through the shared default group.
queue *ARGS:
    pueue add -w "$PWD" -l "why: reproduce honesty-head effects; resolve: native scores and generations match the reference" -- uv run --locked python -m replication.run {{ ARGS }}

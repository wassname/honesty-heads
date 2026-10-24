"""Collect bounded own-model source continuations without authored opposite answers. PI/OpenAI."""

from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import resource
import shlex
import shutil
import subprocess
import sys
import time

from loguru import logger
import torch
import tyro

from .data import append_record
from .model import Config, load_model
from .reference import ROOT, engine
from .run import write_json


def main(baseline_run: Path, out: Path, attempt: int = 1, n_scenarios: int = 400,
         max_new_tokens: int = 256) -> None:
    if not 1 <= attempt <= 3 or n_scenarios <= 0 or max_new_tokens <= 0:
        raise ValueError("Sampling requires attempt1–3 and positive scale/token caps")
    baseline_metadata_bytes = (baseline_run / "metadata.json").read_bytes()
    baseline_metadata = json.loads(baseline_metadata_bytes)
    if baseline_metadata["stage"] != "baseline_collection" or baseline_metadata["status"] != "completed":
        raise ValueError("Use a completed own-model baseline collection")
    baseline_path = baseline_run / "baselines.jsonl"
    baseline_bytes = baseline_path.read_bytes()
    baseline = [json.loads(line) for line in baseline_bytes.splitlines()]
    source = sorted([row for row in baseline if row["split"] == "select"], key=lambda row: row["id"])
    if len({row["id"] for row in baseline}) != len(baseline) or not source:
        raise ValueError("Empty source cohort or duplicate baseline scenarios")
    if any(key in row for row in source for key in ("prefix_ids", "lie_ids", "truth_ids", "source_label", "label")):
        raise ValueError("Inherited decision fields entered own-model sampling")
    selected = source[:n_scenarios]
    config_values = dict(baseline_metadata["config"])
    config_values["coefficient_grid"] = tuple(config_values["coefficient_grid"])
    cfg = Config(**config_values)
    cfg.out, cfg.method, cfg.max_new_tokens = str(out), "source_sampling", max_new_tokens
    out.mkdir(parents=True, exist_ok=False)
    logger.remove()
    logger.add(sys.stdout, level="INFO", format="{message}\n", colorize=False)
    logger.add(out / "run.md", level="DEBUG", format="{message}\n", colorize=False)
    start = time.monotonic()
    metadata = {"stage": "source_sampling", "status": "running", "config": asdict(cfg), "attempt": attempt,
        "argv": shlex.join(sys.argv), "baseline_run": str(baseline_run.resolve()),
        "baseline_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "baseline_metadata_sha256": hashlib.sha256(baseline_metadata_bytes).hexdigest(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
        "selected_ids": [row["id"] for row in selected], "source_cohort_count": len(source),
        "excluded": [{"id": row["id"], "reason": "explicit scale cap"} for row in source[n_scenarios:]],
        "selection": "fixed select split only, sorted IDs; no baseline-label filtering or inherited decision prefix",
        "decoding": {"greedy": False, "temperature": 1.0, "top_p": 1.0, "top_k": 0,
                     "max_new_tokens": max_new_tokens, "thinking": False, "batch_size": 1},
        "semantic_labels_pending": True, "comparison_status": "source continuation preparation only, not paired-token eligibility or transfer",
        "stopping_rule": "attempt indices1–3 only; downstream must deduplicate model/revision/scenario/attempt and select the first eligible opposite; semantic stopping is not implemented here"}
    shutil.copytree(ROOT / "replication", out / "source/replication", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "code", out / "source/code", ignore=shutil.ignore_patterns("__pycache__"))
    (out / "source/baseline_metadata.json").write_bytes(baseline_metadata_bytes)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(ROOT / name, out / "source" / name)
    write_json(out / "metadata.json", metadata)
    logger.info("# Own-model source sampling\n\n```json\n" + json.dumps(metadata, indent=2) + "\n```\n")
    logger.info("SHOULD: saved natural prompt IDs match the own-model baseline exactly; repeated sampling with the same explicit seed returns identical tokens.\n")
    logger.info("TODO validate: a semantically opposite reply may diverge first on style, not on a factual decision. Retain failed pairs and the full scenario cohort.\n")
    try:
        wrapped = load_model(cfg)
        metadata["decoding"]["stop_token_ids"] = sorted(wrapped.stop)
        write_json(out / "precision.json", wrapped.precision)
        for index, row in enumerate(selected):
            ids = engine.prompt_ids(wrapped.tok, row["messages"], row["prefill"])
            if ids != row["input_ids"]:
                raise ValueError(f"Own-model prompt/tokenizer changed: {row['id']}")
            seed = int.from_bytes(hashlib.sha256(f"{cfg.seed}/{cfg.model}/{cfg.revision}/{row['id']}/{attempt}".encode()).digest()[:8], "big")
            tick = time.monotonic()
            generated = wrapped.generate([ids], max_new_tokens, sample=True, temp=1.0, top_p=1.0, top_k=0, seed=seed)[0]
            if index == 0:
                repeated = wrapped.generate([ids], max_new_tokens, sample=True, temp=1.0, top_p=1.0, top_k=0, seed=seed)[0]
                if generated != repeated:
                    raise ValueError("Same-seed native sampled decoding is not reproducible")
                write_json(out / "same_seed_check.json", {"id": row["id"], "seed": seed,
                    "output_ids": generated, "repeat_output_ids": repeated, "exact_match": True})
            reply = wrapped.text(generated)
            append_record(out / "samples.jsonl", {"id": row["id"], "split": "select", "attempt": attempt, "seed": seed,
                "messages": row["messages"], "prefill": row["prefill"], "input_ids": ids, "rendered": row["rendered"],
                "output_ids": generated, "reply": reply, "output_with_special_tokens": wrapped.tok.decode(generated, skip_special_tokens=False),
                "truncated": len(generated) == max_new_tokens and generated[-1] not in wrapped.stop,
                "baseline_reply": row["reply"], "baseline_output_ids": row["output_ids"], "elapsed_seconds": time.monotonic() - tick})
            logger.debug(f"## {row['id']} / attempt{attempt}\n\n```text\n{row['rendered']}\n{reply}\n```\n")
            if (index + 1) % 10 == 0:
                logger.info(f"Recorded {index + 1}/{len(selected)} source continuations.\n")
        records = [json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines()]
        metadata.update(status="completed", records_written=len(records), truncations=sum(row["truncated"] for row in records),
            split_counts=dict(Counter(row["split"] for row in records)), elapsed_seconds=time.monotonic() - start,
            peak_rss_gb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / 1e9,
            peak_cuda_gb=torch.cuda.max_memory_allocated() / 1e9 if cfg.device == "cuda" else 0)
        write_json(out / "metadata.json", metadata)
        logger.info(f"## Result\n\nRecorded {len(records)} source continuations; {metadata['truncations']} token-cap terminations. Opposite labels and token-pair eligibility remain pending.\n\nElapsed {metadata['elapsed_seconds']:.1f}s; peak CUDA {metadata['peak_cuda_gb']:.2f}GB.\n\n{out / 'run.md'}")
    except Exception as error:
        metadata.update(status="failed", error=repr(error), elapsed_seconds=time.monotonic() - start)
        write_json(out / "metadata.json", metadata)
        logger.exception(f"Source sampling failed; no transfer result. {out / 'run.md'}")
        raise


if __name__ == "__main__":
    tyro.cli(main)

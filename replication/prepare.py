"""Collect model-specific baselines without inherited labels or decision prefixes. PI/OpenAI."""

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

from .data import append_record, read_items
from .model import Config, load_model
from .precision import check_tiny_cast_equivalence
from .reference import ROOT, engine
from .run import write_json
from .validation import cached_full_logits, check_decode_steps


def main(model: str, revision: str, out: Path, n_per_split: int = 400,
         max_new_tokens: int = 256, device: str = "cuda", tiny: bool = False,
         dtype: str = "float32", on_demand_float32: bool = True, seed: int = 41) -> None:
    if n_per_split <= 0 or max_new_tokens <= 0:
        raise ValueError("Baseline scale caps must be positive")
    out.mkdir(parents=True, exist_ok=False)
    logger.remove()
    logger.add(sys.stdout, level="INFO", format="{message}\n", colorize=False)
    logger.add(out / "run.md", level="DEBUG", format="{message}\n", colorize=False)
    start = time.monotonic()
    cfg = Config(model=model, revision=revision, out=str(out), dtype=dtype, device=device, tiny=tiny,
                 on_demand_float32=on_demand_float32, seed=seed, method="baseline_collection",
                 n_select_per_class=0, n_test_per_class=0, n_traits=0, random_directions=0,
                 max_new_tokens=max_new_tokens)
    source = ROOT / "data/items_roleplay_insider.json"
    cohort = [item for item in read_items(source) if item["family"] == "rp"]
    selected = [item for split in ("select", "test")
                for item in sorted([item for item in cohort if item["split"] == split], key=lambda item: item["id"])[:n_per_split]]
    if not selected:
        raise ValueError("No scenarios selected for baseline collection")
    ids = {item["id"] for item in selected}
    if len(ids) != len(selected):
        raise ValueError("Duplicate scenario IDs in the baseline cohort")
    metadata = {"stage": "baseline_collection", "status": "running", "config": asdict(cfg),
                "argv": shlex.join(sys.argv), "n_per_split": n_per_split,
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_cohort_counts": dict(Counter(item["split"] for item in cohort)),
                "selected_counts": dict(Counter(item["split"] for item in selected)),
                "excluded": [{"id": item["id"], "split": item["split"], "reason": "explicit scale cap"}
                             for item in cohort if item["id"] not in ids],
                "selection": "family rp and fixed select/test splits; sorted IDs and scale cap only; no9B label/usable filter",
                "semantic_labels_pending": True, "comparison_status": "data preparation only, not a transfer result"}
    shutil.copytree(ROOT / "replication", out / "source/replication", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "code", out / "source/code", ignore=shutil.ignore_patterns("__pycache__"))
    (out / "source/dirty.diff").write_text(subprocess.check_output(["git", "diff", "HEAD", "--", "replication", "code"], cwd=ROOT, text=True))
    metadata["source_hashes"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for directory in (ROOT / "code", ROOT / "replication") for path in sorted(directory.glob("*.py"))}
    metadata["decoding"] = {"greedy": True, "temperature": 0, "thinking": False, "max_new_tokens": max_new_tokens}
    for filename in ("pyproject.toml", "uv.lock", "justfile"):
        shutil.copyfile(ROOT / filename, out / "source" / filename)
    shutil.copyfile(source, out / "source/items_roleplay_insider.json")
    write_json(out / "metadata.json", metadata)
    logger.info("# Model-specific baseline collection\n\n```json\n" + json.dumps(metadata, indent=2) + "\n```\n")
    logger.info("SHOULD: every selected scenario yields one recorded generation with termination status, without old decision-prefix IDs or inherited labels.\n")
    logger.info("TODO validate: semantic labels, opposite continuations and head policy remain separate unfinished work.\n")
    try:
        wrapped = load_model(cfg)
        write_json(out / "model_config.json", wrapped.cfg.to_dict())
        write_json(out / "precision.json", wrapped.precision)
        metadata["decoding"]["stop_token_ids"] = sorted(wrapped.stop)
        write_json(out / "metadata.json", metadata)
        first = selected[0]
        if tiny and on_demand_float32:
            natural_case = {"dec": first["dec"], "prefill": first["prefill"], "prefix_ids": []}
            write_json(out / "storage_cast_check.json", check_tiny_cast_equivalence(wrapped, natural_case))
        input_ids = engine.prompt_ids(wrapped.tok, first["dec"], first["prefill"])
        cached, complete = cached_full_logits(wrapped, input_ids)
        tolerance = 2e-4 if dtype == "float32" else 0.04
        torch.testing.assert_close(cached, complete, rtol=tolerance, atol=tolerance)
        write_json(out / "cache_check.json", {"id": first["id"], "all_vocabulary_logit_max_error": float((cached - complete).abs().max()),
                                             "scope": "one unsteered natural-prompt cached/full step"})
        write_json(out / "natural_decode_check.json", check_decode_steps(wrapped, first, {"units": [], "delta": {}}, 0.0, dtype))
        for index, item in enumerate(selected):
            tick = time.monotonic()
            tokens = engine.prompt_ids(wrapped.tok, item["dec"], item["prefill"])
            generated = wrapped.generate([tokens], max_new_tokens)[0]
            reply = wrapped.text(generated)
            append_record(out / "baselines.jsonl", {"id": item["id"], "split": item["split"], "family": item["family"],
                "messages": item["dec"], "prefill": item["prefill"], "input_ids": tokens,
                "rendered": wrapped.tok.decode(tokens, skip_special_tokens=False), "output_ids": generated,
                "reply": reply, "output_with_special_tokens": wrapped.tok.decode(generated, skip_special_tokens=False),
                "truncated": len(generated) == max_new_tokens and generated[-1] not in wrapped.stop,
                "elapsed_seconds": time.monotonic() - tick})
            logger.debug(f"## {item['id']} / {item['split']}\n\n```text\n{wrapped.tok.decode(tokens)}\n{reply}\n```\n")
            if (index + 1) % 10 == 0:
                logger.info(f"Recorded {index + 1}/{len(selected)} model-specific baselines.\n")
        records = [json.loads(line) for line in (out / "baselines.jsonl").read_text().splitlines()]
        metadata.update(status="completed", records_written=len(selected), truncations=sum(row["truncated"] for row in records), elapsed_seconds=time.monotonic() - start,
                        peak_rss_gb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / 1e9,
                        peak_cuda_gb=torch.cuda.max_memory_allocated() / 1e9 if device == "cuda" else 0)
        write_json(out / "metadata.json", metadata)
        logger.info(f"## Result\n\nRecorded {len(selected)} baselines: {metadata['selected_counts']}. Semantic labels remain pending.\n\nElapsed {metadata['elapsed_seconds']:.1f}s; peak CUDA {metadata['peak_cuda_gb']:.2f}GB.\n\n{out / 'run.md'}")
    except Exception as error:
        metadata.update(status="failed", error=repr(error), elapsed_seconds=time.monotonic() - start)
        write_json(out / "metadata.json", metadata)
        logger.exception(f"Baseline collection failed; no transfer conclusion. {out / 'run.md'}")
        raise


if __name__ == "__main__":
    tyro.cli(main)

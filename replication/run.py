"""Run the real extraction, intervention, scoring, and generation pipeline. PI/OpenAI."""

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import resource
import subprocess
import sys
import time
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch
import tyro
from loguru import logger
from tabulate import tabulate

from .calibration import calibration_identity, direction_digest, fit_coefficient, frozen_coefficient
from .data import append_record, generation_items, margin, persona_items, prompt_record, read_items, roleplay_items, sequence
from .uncertainty import trait_question_intervals
from .methods import (
    contextual_query_heads, extract_query, extract_residual, extract_vjp, head_reference, intervention,
    random_head_reference, random_vector, reference_vjp_layers,
)
from .model import Config, load_model, vjp_sites
from .precision import check_tiny_cast_equivalence
from .recompute import check_recomputed_gradients
from .reference import ROOT, audit, engine, persona_rep, persona_stats
from .report_traits import TRAITS
from .shuffled import save_shuffled_direction, shuffled_direction
from .validation import check_batch_invariance, check_decode_steps, check_head_intervention, check_query_local_edits, check_reference_engine, check_vjp_derivative, validate_intervention


EVAL_VERSION = "upstream-roleplay-persona-v5-natural-generation-validation"


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def logp_rows(wrapped, items: list[dict], batch_size: int) -> torch.Tensor:
    return wrapped.last_logp([sequence(wrapped.tok, it) for it in items], bs=batch_size)


def score_items(wrapped, items: list[dict], name: str, cfg: Config, out: Path) -> list[float]:
    scores = []
    start = time.monotonic()
    for offset in range(0, len(items), cfg.batch_size):
        batch = items[offset:offset + cfg.batch_size]
        logp = logp_rows(wrapped, batch, cfg.batch_size)
        for item, row in zip(batch, logp, strict=True):
            value = margin(row, item["lie_ids"], item["truth_ids"])
            scores.append(value)
            truth_probability = float(row[item["truth_ids"]].exp().sum())
            lie_probability = float(row[item["lie_ids"]].exp().sum())
            top_token = int(row.argmax())
            append_record(out / "scores.jsonl", {"condition": name, "id": item["id"], "margin_bits": value,
                "truth_probability": truth_probability, "lie_probability": lie_probability,
                "pair_probability_mass": truth_probability + lie_probability,
                "truth_is_top_token": top_token in item["truth_ids"], "lie_is_top_token": top_token in item["lie_ids"],
                "top_token": top_token, "top_text": wrapped.tok.decode([top_token])})
        del logp
    elapsed = time.monotonic() - start
    append_record(out / "timings.jsonl", {"condition": name, "n_items": len(items), "seconds": elapsed})
    logger.info(f"Scored {name}: {len(items)} items in {elapsed:.1f}s; item scores saved.\n")
    return scores


def generate_examples(wrapped, items: list[dict], name: str, cfg: Config, out: Path) -> None:
    for item in generation_items(items, cfg.n_generation_per_stratum):
        started = time.monotonic()
        ids = engine.prompt_ids(wrapped.tok, item["dec"], item["prefill"])
        new = wrapped.generate([ids], cfg.max_new_tokens)[0]
        text = wrapped.text(new)
        append_record(out / "generations.jsonl", {
            "condition": name, "id": item["id"], "messages": item["dec"], "prefill": item["prefill"],
            "input_ids": ids, "rendered": wrapped.tok.decode(ids, skip_special_tokens=False),
            "output_ids": new, "output_with_special_tokens": wrapped.tok.decode(new, skip_special_tokens=False),
            "reply": text, "grade_rule": item["grade"], "rule_grade": audit.grade_lie(text, item),
            "truth_candidates": item["truth"], "lie_candidates": item["lie"], "source_label": item["label"],
            "source_answer_polarity": persona_rep.polarity(item["lie"] if item["label"] == "lie" else item["truth"]),
            "truncated": len(new) == cfg.max_new_tokens and new[-1] not in wrapped.stop,
            "elapsed_seconds": time.monotonic() - started,
        })
        logger.debug(f"## generation -> {name} -> {item['id']}\n\n```text\n{wrapped.tok.decode(ids)}\n{text}\n```\n")


def generate_persona_examples(wrapped, items: list[dict], name: str, cfg: Config, out: Path) -> None:
    for _, trait in TRAITS:
        for half in ("Yes", "No"):
            chosen = [item for item in items if item["family"] == trait and item["match"] == half]
            for item in chosen[:cfg.n_persona_generation_per_half]:
                ids = engine.prompt_ids(wrapped.tok, item["dec"], item["prefill"])
                tokens = wrapped.generate([ids], cfg.max_new_tokens)[0]
                reply = wrapped.text(tokens)
                first = re.match(r"\s*(yes|no)\b", reply, flags=re.IGNORECASE)
                polarity = first.group(1).capitalize() if first is not None else "other"
                append_record(out / "persona_generations.jsonl", {"condition": name, "id": item["id"],
                    "trait": trait, "trait_matching_polarity": half, "messages": item["dec"], "prefill": item["prefill"],
                    "input_ids": ids, "output_ids": tokens, "reply": reply, "first_yes_no": polarity,
                    "trait_endorsement": polarity == half if polarity != "other" else None,
                    "truncated": len(tokens) == cfg.max_new_tokens and tokens[-1] not in wrapped.stop,
                    "selection": "first source question in each highlighted trait/Yes-No half"})
                logger.debug(f"## persona reply -> {name} -> {item['id']}\n\n```text\n{wrapped.tok.decode(ids)}\n{reply}\n```\n")


def ordinary_controls(wrapped, name: str, cfg: Config, out: Path) -> None:
    items = read_items(ROOT / "data/controls.json")
    groups = ("honest_tf", "honest_qa", "french", "caps", "json")
    for group in groups:
        candidates = [it for it in items if it["group"] == group]
        n_polarities = len({str(it.get("polarity")) for it in candidates})
        chosen = audit.cap_groups(candidates, cfg.n_controls_per_polarity * n_polarities)
        for item in chosen:
            ids = engine.prompt_ids(wrapped.tok, item["messages"])
            tokens = wrapped.generate([ids], cfg.max_new_tokens)[0]
            reply = wrapped.text(tokens)
            append_record(out / "controls.jsonl", {"condition": name, "id": item["id"], "group": group,
                "messages": item["messages"], "input_ids": ids, "reply": reply, "output_ids": tokens, "polarity": item.get("polarity"),
                "grade_rule": item["grade"], "correct": audit.grade_control(reply, item["grade"])})


def polarity_summary(items: list[dict], before: list[float], after: list[float]) -> dict:
    change = np.array(before) - np.array(after)
    groups = {pol: [i for i, it in enumerate(items) if it["label"] == "lie"
                    and persona_rep.polarity(it["lie"]) == pol] for pol in ("yes", "no", "other")}
    means = {pol: float(change[ix].mean()) if ix else None for pol, ix in groups.items()}
    truth = [i for i, it in enumerate(items) if it["label"] == "resist" and before[i] < 0]
    lies = [i for i, it in enumerate(items) if it["label"] == "lie" and before[i] > 0]
    return {"n_by_lie_polarity": {pol: len(ix) for pol, ix in groups.items()}, "gain_bits": means,
            "balanced_gain_bits": (means["yes"] + means["no"]) / 2 if means["yes"] is not None and means["no"] is not None else None,
            "baseline_lies": len(lies), "truth_beats_lie": sum(after[i] < 0 for i in lies),
            "baseline_truthful": len(truth), "truthful_retained": sum(after[i] < 0 for i in truth)}


def evaluate_shuffled(wrapped, select, test, persona, selection_base, test_base, persona_base,
                      fitted, method: str, coefficient: float, cfg: Config, out: Path, destination: Path) -> None:
    direction, provenance = shuffled_direction(wrapped, select, fitted, method, cfg)
    save_shuffled_direction(direction, destination / "shuffled_direction.safetensors")
    with intervention(wrapped, direction, coefficient):
        selected = score_items(wrapped, select, f"{method}/shuffled/select", cfg, out)
        scored = score_items(wrapped, test, f"{method}/shuffled/test", cfg, out)
        values = score_items(wrapped, persona, f"{method}/shuffled/persona", cfg, out)
    record = {"names": ["base", "set"], "items": [
        {"id": item["id"], "trait": item["family"], "match": item["match"], "m": [base, value]}
        for item, base, value in zip(persona, persona_base, values, strict=True)]}
    write_json(destination / "shuffled_control.json", {"provenance": provenance, "coefficient": coefficient,
        "select": polarity_summary(select, selection_base, selected), "test": polarity_summary(test, test_base, scored),
        "persona": record, "generation_evaluated": False,
        "interpretation": "one norm-matched label permutation at fixed strength, not a null distribution or a matched-honesty intervention"})
    logger.info(f"Shuffled-label diagnostic {method}: {provenance['changed_labels']} labels changed, max norm error {provenance['max_per_unit_norm_error']:.3g}.\n")


def save_persona_stats(out: Path) -> None:
    arguments = sys.argv
    captured = io.StringIO()
    try:
        sys.argv = ["persona_stats.py", str(out / "persona.json"), str(out / "persona_stats.json")]
        with redirect_stdout(captured):
            persona_stats.main()
    finally:
        sys.argv = arguments
    logger.debug("## persona -> upstream statistics\n\n```text\n" + captured.getvalue() + "```\n")


def run_methods(wrapped, select: list[dict], test: list[dict], persona: list[dict], cfg: Config, out: Path) -> list[dict]:
    logger.info("## baseline -> fixed source scenarios\n")
    for item in select + test + persona:
        append_record(out / "prompts.jsonl", prompt_record(wrapped.tok, item))
    selection_base = score_items(wrapped, select, "base/select", cfg, out)
    test_base = [] if cfg.calibration_only else score_items(wrapped, test, "base/test", cfg, out)
    persona_base = [] if cfg.calibration_only else score_items(wrapped, persona, "base/persona", cfg, out)
    baseline_items = select if cfg.calibration_only else select + test
    baseline_mismatch = [{"id": it["id"], "source_label": it["label"], "margin_bits": m}
                         for it, m in zip(baseline_items, selection_base + test_base, strict=True)
                         if (m > 0) != (it["label"] == "lie")]
    write_json(out / "baseline_label_disagreements.json", baseline_mismatch)
    logger.info(f"Baseline source-label disagreements: {len(baseline_mismatch)}/{len(baseline_items)}. Retained and logged, not silently filtered.\n")
    if not cfg.calibration_only:
        generate_examples(wrapped, test, "base", cfg, out)
        generate_persona_examples(wrapped, persona, "base", cfg, out)
        ordinary_controls(wrapped, "base", cfg, out)
    head = head_reference(wrapped, select, cfg.tiny, cfg.span)
    methods = ["heads", "heads_full_attention", "mean_diff", "spherical", "vjp_resid", "vjp_reference", "query_steer"] if cfg.method == "all" else [cfg.method]
    results = []
    for method in methods:
        stage = time.monotonic()
        destination = out / method
        destination.mkdir()
        logger.info(f"## extraction -> {method}\n")
        residual_site = min(cfg.layer - 1, wrapped.L - 1)
        source_site, target_site = vjp_sites(cfg, wrapped.L)
        if method == "heads":
            direction = head
        elif method == "heads_full_attention":
            direction = {"delta": head["delta"], "units": contextual_query_heads(wrapped, head["units"])}
        elif method in ("mean_diff", "spherical"):
            direction = extract_residual(wrapped, select, method, residual_site, cfg.span)
        elif method in ("vjp_resid", "vjp_reference"):
            source_site = reference_vjp_layers(wrapped.L) if method == "vjp_reference" else source_site
            if cfg.tiny and cfg.checkpoint_vjp:
                source_layers = source_site if isinstance(source_site, tuple) else (source_site,)
                write_json(destination / "recomputation_check.json", check_recomputed_gradients(wrapped, select, source_layers, target_site))
            direction = extract_vjp(wrapped, select, source_site, target_site)
        elif method == "query_steer":
            direction = extract_query(wrapped, select, head["units"])
        else:
            raise ValueError(method)
        coeff = 0.02 if method == "spherical" and cfg.method == "all" else cfg.coeff
        if not isinstance(direction, dict):
            direction.save(str(destination / "direction.safetensors"))
        if cfg.calibration_only:
            if method == "vjp_reference":
                probe_strength = min(abs(value) for value in cfg.coefficient_grid if value != 0)
                checks = validate_intervention(wrapped, select[0], direction, probe_strength, cfg.dtype)
                checks["joint_vjp_derivative"] = check_vjp_derivative(wrapped, select[0], source_site, target_site)
                checks["natural_decode"] = check_decode_steps(wrapped, select[0], direction, probe_strength, cfg.dtype)
                if cfg.tiny:
                    checks["per_source_vjp_derivatives"] = {layer: check_vjp_derivative(wrapped, select[0], layer, target_site)
                                                            for layer in source_site}
                write_json(destination / "runtime_checks.json", checks)
                logger.debug(f"Selection-only VJP coverage checks: {checks}\n")
            record = fit_coefficient(wrapped, cfg, select, selection_base, direction, head,
                lambda name: score_items(wrapped, select, f"{method}/calibration/{name}", cfg, out))
            write_json(destination / "calibration.json", record)
            logger.info(f"Calibration {method}: {record['status']}; target {record['target_gain_bits']:.4f} bits.\n")
            if method == "vjp_reference":
                probes = {min(cfg.coefficient_grid), 0.0, max(cfg.coefficient_grid)}
                if record["coefficient"] is not None:
                    probes.add(record["coefficient"])
                for strength in sorted(probes):
                    with intervention(wrapped, direction, strength):
                        generate_examples(wrapped, select, f"{method}/selection-probe/{strength:g}", cfg, out)
                write_json(destination / "source_probe_policy.json", {"strengths": sorted(probes),
                    "scope": "fixed selection examples only; no held-out or persona outcomes", "coefficient_retuned_from_replies": False})
            continue
        if cfg.calibration_path is not None:
            record = json.loads(Path(cfg.calibration_path).read_text())
            coeff = frozen_coefficient(record, calibration_identity(wrapped, cfg, select, selection_base, direction, head))
            write_json(destination / "calibration.json", record)
            logger.info(f"Frozen coefficient loaded; positive selection-score match eligible: {record['eligible_for_positive_selection_score_match']}. Held-out matching is not established by freezing.\n")
        resolved_method = {"method": method, "coefficient": coeff, "residual_input_boundary": residual_site + 1,
                           "vjp_source_output_layer": source_site, "vjp_target_output_layer": target_site,
                           "mean_extraction_span": cfg.span, "random_directions": cfg.random_directions,
                           "dtype": str(wrapped.compute_dtype),
                           "selected_heads": direction["units"] if isinstance(direction, dict) else contextual_query_heads(wrapped, head["units"]) if method == "query_steer" else None,
                           "vector_config": None if isinstance(direction, dict) else direction.cfg.to_dict(),
                           "direction_sha256": direction_digest(direction),
                           "calibration_sha256": hashlib.sha256((destination / "calibration.json").read_bytes()).hexdigest()
                               if cfg.calibration_path else None}
        write_json(destination / "resolved_config.json", resolved_method)
        logger.debug(f"## {method} -> resolved configuration\n\n```json\n{json.dumps(resolved_method, indent=2)}\n```\n")
        checks = validate_intervention(wrapped, test[0], direction, coeff, cfg.dtype)
        checks["multi_step_natural_decode"] = {item["id"]: check_decode_steps(wrapped, item, direction, coeff, cfg.dtype)
                                              for item in generation_items(test, 1)}
        if cfg.batch_size > 1:
            checks["batch_invariance"] = check_batch_invariance(wrapped, test, direction, coeff, cfg.dtype)
        if method == "query_steer":
            checks["direct_query_edits"] = check_query_local_edits(wrapped, test[0], direction, coeff)
        if method in ("vjp_resid", "vjp_reference"):
            checks["vjp_derivative"] = check_vjp_derivative(wrapped, test[0], source_site, target_site)
            if method == "vjp_reference" and cfg.tiny:
                checks["per_source_vjp_derivatives"] = {layer: check_vjp_derivative(wrapped, test[0], layer, target_site)
                                                        for layer in source_site}
        if method == "heads" and wrapped.cfg.model_type == "qwen3_5_text":
            checks["reference_steered"] = check_head_intervention(wrapped, test[0], direction, coeff)
        write_json(destination / "runtime_checks.json", checks)
        if "reference_steered" in checks:
            reference_steered = checks["reference_steered"]
            torch.testing.assert_close(torch.tensor(reference_steered["native_margin_bits"]),
                                       torch.tensor(reference_steered["reference_full_margin_bits"]), rtol=2e-3, atol=2e-3)
            if reference_steered["full_logp_max_error"] > 2e-3:
                raise AssertionError(f"nonzero head edit disagrees with full reference: {reference_steered}")
        logger.debug(f"Runtime checks for {method}: {checks}\n")
        seeds = range(cfg.random_start, cfg.random_start + cfg.random_directions)
        names = ["base", "set"] + [f"random{i}" for i in seeds]
        columns = [persona_base]
        conditions = [direction] + [random_head_reference(wrapped, direction, i) if isinstance(direction, dict)
                                   else random_vector(direction, i) for i in seeds]
        for name, current in zip(names[1:], conditions, strict=True):
            with intervention(wrapped, current, coeff):
                selection = score_items(wrapped, select, f"{method}/{name}/select", cfg, out)
                scored = score_items(wrapped, test, f"{method}/{name}/test", cfg, out)
                columns.append(score_items(wrapped, persona, f"{method}/{name}/persona", cfg, out))
                partial = {"names": names[:len(columns)], "planned_names": names,
                           "items": [{"id": it["id"], "trait": it["family"], "match": it["match"],
                                      "m": [column[i] for column in columns]} for i, it in enumerate(persona)]}
                write_json(destination / "persona.partial.json", partial)
                if name == "set":
                    generate_examples(wrapped, test, method, cfg, out)
                    generate_persona_examples(wrapped, persona, method, cfg, out)
                    ordinary_controls(wrapped, method, cfg, out)
                current_summary = {"method": method, "condition": name, "coefficient": coeff,
                                   "select": polarity_summary(select, selection_base, selection),
                                   "test": polarity_summary(test, test_base, scored)}
                append_record(out / "honesty.jsonl", current_summary)
                if name == "set":
                    results.append(current_summary)
            if name == "set" and cfg.shuffled_seed is not None:
                evaluate_shuffled(wrapped, select, test, persona, selection_base, test_base, persona_base,
                                  direction, method, coeff, cfg, out, destination)
        matrix = np.array(columns).T
        record = {"names": names, "items": [{"id": it["id"], "trait": it["family"], "match": it["match"], "m": row.tolist()}
                                            for it, row in zip(persona, matrix, strict=True)]}
        write_json(destination / "persona.json", record)
        write_json(destination / "question_intervals.json", trait_question_intervals(record))
        if cfg.random_directions:
            save_persona_stats(destination)
        statistics = "written" if cfg.random_directions else "not requested"
        logger.info(f"{method}: {time.monotonic() - stage:.1f}s; {len(persona)} persona items; random-direction statistics {statistics}.\n")
    return results


def main(cfg: Config) -> None:
    out = Path(cfg.out)
    out.mkdir(parents=True, exist_ok=False)
    logger.remove()
    logger.add(sys.stdout, level="INFO", format="{message}\n", colorize=False)
    logger.add(out / "run.md", level="DEBUG", format="{message}\n", colorize=False)
    start = time.monotonic()
    metadata = {}
    try:
        metadata = {"config": asdict(cfg), "argv": shlex.join(sys.argv), "eval_version": EVAL_VERSION,
                    "runtime_environment": {key: os.environ[key] for key in
                        ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "PYTORCH_ALLOC_CONF", "PYTORCH_CUDA_ALLOC_CONF") if key in os.environ},
                    "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                    "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()),
                    "upstream_commit": "6c658755c400b95806bef2bca3f1cabfbddab342",
                    "steering_lite_commit": "3d242756359bc5575275f30635a0ddaf3ce0ec08",
                    "comparison_status": "selection-only calibration" if cfg.calibration_only else
                    "frozen selection coefficient; inspect match eligibility, achieved held-out gains and generated answers" if cfg.calibration_path else
                    "unmatched fixed-strength diagnostic"}
        shutil.copytree(ROOT / "replication", out / "source/replication", ignore=shutil.ignore_patterns("__pycache__"))
        for filename in ("pyproject.toml", "uv.lock", "justfile"):
            shutil.copyfile(ROOT / filename, out / "source" / filename)
        write_json(out / "metadata.json", metadata)
        logger.info("# Honesty steering replication\n\n```json\n" + json.dumps(metadata, indent=2) + "\n```\n")
        logger.info("Tiny random-model scores test execution only. No quality or replication claim follows.\n" if cfg.tiny else "Reference run: numerical/backend differences are explicit; source-label disagreements are retained.\n")
        logger.info("SHOULD: coefficient zero leaves scores unchanged within float32 roundoff; a larger change invalidates the intervention path.\n")
        logger.info("TODO validate: held-out honesty gains survive Yes/No stratification and appear in generated replies.\n")
        if not cfg.tiny and cfg.model != "Qwen/Qwen3.5-9B":
            raise NotImplementedError("Real model transfer requires rebuilt model-specific token IDs and labels; the transfer stage is not implemented yet")
        wrapped = load_model(cfg)
        write_json(out / "model_config.json", wrapped.cfg.to_dict())
        parameter_bytes = {}
        for parameter in wrapped.m.parameters():
            device = str(parameter.device)
            parameter_bytes[device] = parameter_bytes.get(device, 0) + parameter.numel() * parameter.element_size()
        placement = {"checkpoint_source": wrapped.checkpoint_source, "precision": wrapped.precision,
                     "parameter_bytes_by_device": parameter_bytes,
                     "head_projection_devices": [str(wrapped.head_site(l)[0].weight.device) for l in range(wrapped.L)],
                     "peak_rss_gb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / 1e9}
        write_json(out / "placement.json", placement)
        logger.info(f"Model placement: {placement['parameter_bytes_by_device']}; peak RSS {placement['peak_rss_gb']:.2f}GB.\n")
        select = roleplay_items("select", cfg.n_select_per_class)
        test = roleplay_items("test", cfg.n_test_per_class)
        if cfg.on_demand_float32 and cfg.tiny:
            write_json(out / "storage_cast_check.json", check_tiny_cast_equivalence(wrapped, test[0]))
        if wrapped.cfg.model_type == "qwen3_5_text" and not cfg.calibration_only:
            reference_check = check_reference_engine(wrapped, test[0], cfg.span)
            write_json(out / "reference_engine_check.json", reference_check)
            logger.info(f"Native/upstream-engine check: {reference_check}\n")
            torch.testing.assert_close(torch.tensor(reference_check["native_margin_bits"]),
                                       torch.tensor(reference_check["reference_prepared_margin_bits"]), rtol=2e-3, atol=2e-3)
            if reference_check["native_reference_logp_max_error"] > 2e-3:
                raise AssertionError(f"unsteered full-vocabulary reference discrepancy: {reference_check}")
        persona = persona_items(cfg.n_traits, cfg.n_persona_per_half)
        for label in ("resist", "lie"):
            example = next(it for it in select if it["label"] == label)
            logger.debug(f"## extraction -> source {label}\n\n```json\n{json.dumps(prompt_record(wrapped.tok, example), ensure_ascii=False, indent=2)}\n```\n")
        results = run_methods(wrapped, select, test, persona, cfg, out)
        metadata.update(elapsed_seconds=time.monotonic() - start,
                        peak_cuda_gb=torch.cuda.max_memory_allocated() / 1e9 if cfg.device == "cuda" else 0,
                        peak_rss_gb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / 1e9,
                        peak_cuda_reserved_gb=torch.cuda.max_memory_reserved() / 1e9 if cfg.device == "cuda" else 0,
                        status="completed")
        write_json(out / "metadata.json", metadata)
        write_json(out / "summary.json", results)
        rows = [[r["method"], r["test"]["truth_beats_lie"], r["test"]["baseline_lies"],
                 r["test"]["balanced_gain_bits"], r["test"]["truthful_retained"], r["test"]["baseline_truthful"]] for r in results]
        logger.info(f"## Result\n\n{tabulate(rows, headers=['method', 'truth beats lie↑', 'baseline lies', 'balanced gain↑ bits', 'truthful retained↑', 'baseline truthful'], tablefmt='pipe')}\n\nElapsed: {metadata['elapsed_seconds']:.1f}s; peak CUDA: {metadata['peak_cuda_gb']:.2f}GB.\n\n{out / 'run.md'}")
    except Exception as error:
        metadata.update(status="failed", error=repr(error), elapsed_seconds=time.monotonic() - start,
                        peak_cuda_gb=torch.cuda.max_memory_allocated() / 1e9 if torch.cuda.is_initialized() else 0,
                        peak_rss_gb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / 1e9)
        write_json(out / "metadata.json", metadata)
        logger.exception("```text\nPipeline failed; inspect the traceback and preserved artifacts.\n")
        logger.error(f"```\n\nStatus: failed.\n\n{out / 'run.md'}")
        raise


if __name__ == "__main__":
    main(tyro.cli(Config))

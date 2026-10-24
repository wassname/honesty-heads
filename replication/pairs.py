"""Derive own-model first-divergence records from reviewed native continuations. PI/OpenAI."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil

from transformers import AutoTokenizer
import tyro

from .data import sequence
from .reference import ROOT, engine, persona_rep
from .run import write_json


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique_rows(path: Path) -> dict:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    indexed = {row["id"]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError(f"Duplicate scenarios: {path}")
    return indexed


def main(baseline_run: Path, reviews: list[Path], out: Path) -> None:
    metadata_path = baseline_run / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    if metadata["stage"] != "baseline_collection" or metadata["status"] != "completed" or metadata["config"]["tiny"]:
        raise ValueError("Use a completed trained own-model baseline collection")
    baseline_path = baseline_run / "baselines.jsonl"
    baseline = unique_rows(baseline_path)
    source_ids = {ident for ident, row in baseline.items() if row["split"] == "select"}
    model, revision = (metadata["config"][key] for key in ("model", "revision"))
    tok = AutoTokenizer.from_pretrained(model, revision=revision)
    batches = [(json.loads((directory / "manifest.json").read_text()), directory) for directory in reviews]
    attempts = sorted(manifest["attempt"] for manifest, _ in batches)
    if not attempts or attempts != list(range(1, max(attempts) + 1)) or max(attempts) > 3:
        raise ValueError("Review each attempt once, consecutively from1, at most3")
    selected, histories, lineage = {}, {ident: [] for ident in source_ids}, []
    for manifest, directory in sorted(batches, key=lambda entry: entry[0]["attempt"]):
        attempt = manifest["attempt"]
        sample_run = Path(manifest["sample_run"])
        sample_path = sample_run / "samples.jsonl"
        sampled = unique_rows(sample_path)
        sampling = json.loads((sample_run / "metadata.json").read_text())
        if any(manifest[key] != value or sampling["config"][key] != value for key, value in (("model", model), ("revision", revision))):
            raise ValueError("Reviewed samples belong to a different model/revision")
        if sampling["status"] != "completed" or sampling["attempt"] != attempt or sampling["config"]["tiny"]:
            raise ValueError("Review must identify a completed trained sampling attempt")
        if set(sampled) != source_ids or set(manifest["source_ids"]) != source_ids:
            raise ValueError("Every fixed source scenario must remain in the attempt")
        if digest(baseline_path) != manifest["baseline_sha256"] or digest(baseline_path) != sampling["baseline_sha256"]:
            raise ValueError("Baseline records changed after sampling or review")
        if digest(metadata_path) != sampling["baseline_metadata_sha256"] or digest(sample_path) != manifest["sample_sha256"]:
            raise ValueError("Baseline metadata or sampled continuations changed")
        decision_path = directory / "pair_decisions.json"
        decisions = json.loads(decision_path.read_text())
        compiled_path, packet_path = directory / "compiled.json", directory / "token_review_packet.json"
        if digest(compiled_path) != decisions["compiled_sha256"] or digest(packet_path) != decisions["token_packet_sha256"]:
            raise ValueError("Reviewed semantic or token packet changed")
        if digest(Path(decisions["review_path"])) != decisions["review_sha256"]:
            raise ValueError("Semantic/token review changed after parent decisions")
        compiled = json.loads(compiled_path.read_text())
        for path, expected in compiled["input_sha256"].items():
            if digest(ROOT / path) != expected:
                raise ValueError("Primary baseline classifications or source eligibility changed")
        if compiled["attempt"] != attempt or compiled["model"] != model or compiled["revision"] != revision:
            raise ValueError("Semantic compilation belongs to another model/attempt")
        cases = {row["id"]: row for row in compiled["cases"]}
        if len(cases) != len(compiled["cases"]) or set(cases) != source_ids:
            raise ValueError("Incomplete or duplicated semantic review cohort")
        candidate_rows = json.loads(packet_path.read_text())["candidates"]
        candidates = {row["id"]: row for row in candidate_rows}
        stop = set(sampling["decoding"]["stop_token_ids"])
        expected_candidates, mechanical_exclusions = set(), {}
        for ident in source_ids:
            case = cases[ident]
            first, second = baseline[ident]["output_ids"], sampled[ident]["output_ids"]
            divergence = next((i for i, (a, b) in enumerate(zip(first, second)) if a != b), None)
            if case["stable_strict_opposite"]:
                if divergence is None:
                    mechanical_exclusions[ident] = "no differing token before a sequence ends"
                elif first[divergence] in stop or second[divergence] in stop:
                    mechanical_exclusions[ident] = "stop-token divergence, not a factual contrast"
                else:
                    expected_candidates.add(ident)
            if case["token_review_candidate"] != (ident in expected_candidates) or not case["withholding_reason"].strip():
                raise ValueError("Semantic compilation disagrees with mechanical eligibility or lacks a withholding reason")
        if len(candidates) != len(candidate_rows) or candidates.keys() != expected_candidates:
            raise ValueError("Token packet omits an eligible semantic opposite or adds an ineligible scenario")
        approved, rejected = set(decisions["pairs"]), set(decisions["rejected"])
        if approved & rejected or approved | rejected != candidates.keys():
            raise ValueError("Every unique token candidate needs one explicit approval or rejection")
        if any(not reason.strip() for reason in decisions["rejected"].values()):
            raise ValueError("Rejected token candidates need a reason")
        for ident in sorted(source_ids):
            row, original, case = sampled[ident], baseline[ident], cases[ident]
            if row["attempt"] != attempt or row["split"] != "select":
                raise ValueError("Sample identity/attempt mismatch")
            for key in ("messages", "prefill", "input_ids", "rendered"):
                if row[key] != original[key]:
                    raise ValueError(f"Sampled natural prompt differs: {ident}/{key}")
            if (row["baseline_reply"], row["baseline_output_ids"]) != (original["reply"], original["output_ids"]):
                raise ValueError("Sampled baseline link differs")
            if ident not in decisions["pairs"]:
                reason = (decisions["rejected"][ident] if ident in rejected else mechanical_exclusions[ident]
                          if ident in mechanical_exclusions else case["withholding_reason"])
                histories[ident].append({"attempt": attempt, "selected": False, "reason": reason})
                continue
            if not case["stable_strict_opposite"]:
                raise ValueError("Approved pair is not a stable reviewed semantic opposite")
            decision, candidate = decisions["pairs"][ident], candidates[ident]
            if decision["proxy_kind"] not in ("direct", "indirect"):
                raise ValueError("Unknown first-divergence proxy subtype")
            branches = {branch["label"]: branch for branch in candidate["branches"]}
            if len(branches) != len(candidate["branches"]) or len(branches) != 2:
                raise ValueError("A token contrast requires two unique branches")
            truth_branch = branches[decision["truth_branch"]]
            truth_is_baseline = case["primary_baseline_class"] == "true"
            truth, lie = (original, row) if truth_is_baseline else (row, original)
            baseline_class = case["primary_baseline_class"]
            if baseline_class not in ("true", "false") or case["baseline_judgment"]["verdict"] != baseline_class or case["sample_judgment"]["verdict"] != ("false" if truth_is_baseline else "true"):
                raise ValueError("Reviewed continuations are not opposite binary central answers")
            if (truth_branch["reply"], truth_branch["truncated"]) != (truth["reply"], truth["truncated"]):
                raise ValueError("Reviewed orientation differs from baseline/sample semantic classes")
            first, second = original["output_ids"], row["output_ids"]
            divergence = next((i for i, (a, b) in enumerate(zip(first, second)) if a != b), None)
            if divergence is None or first[divergence] in stop or second[divergence] in stop:
                raise ValueError("Missing or stop-only divergence")
            prefix = first[:divergence]
            if prefix != candidate["shared_generated_prefix_ids"] or prefix != second[:divergence]:
                raise ValueError("Reviewed generated prefix differs")
            if engine.prompt_ids(tok, original["messages"], original["prefill"]) != original["input_ids"]:
                raise ValueError("Pinned tokenizer no longer reproduces the natural prompt")
            if (candidate["messages"], candidate["prefill"]) != (original["messages"], original["prefill"]) or candidate["conditioning_text"] != tok.decode(original["input_ids"] + prefix, skip_special_tokens=False):
                raise ValueError("Token-review conditioning differs from the observed prompt/history")
            for branch in branches.values():
                origins = [origin for origin in (original, row) if (branch["reply"], branch["truncated"]) == (origin["reply"], origin["truncated"])]
                if len(origins) != 1 or branch["divergent_token_id"] != origins[0]["output_ids"][divergence] or branch["divergent_token_text"] != tok.decode([branch["divergent_token_id"]], skip_special_tokens=False):
                    raise ValueError("Token-review branch differs from the original generated token")
            paired = {"id": ident, "source": "own_model_native_pair", "split": "select", "family": "rp",
                "dec": original["messages"], "prefill": original["prefill"], "prefix_ids": prefix, "prefix": tok.decode(prefix),
                "truth_ids": [truth["output_ids"][divergence]], "lie_ids": [lie["output_ids"][divergence]],
                "truth": [tok.decode([truth["output_ids"][divergence]])], "lie": [tok.decode([lie["output_ids"][divergence]])],
                "truth_completion": truth["reply"], "lie_completion": lie["reply"], "dec_reply": original["reply"],
                "pair_reply": row["reply"], "label": "resist" if truth_is_baseline else "lie", "usable": True,
                "grade": "pair", "dpos": divergence, "pair_sample": attempt, "first": "truth" if truth_is_baseline else "lie",
                "proxy_kind": decision["proxy_kind"], "qualifications": decision["qualifications"],
                "provenance": {"model": model, "revision": revision, "attempt": attempt, "seed": row["seed"],
                    "sample_sha256": digest(sample_path), "parent_decisions_sha256": digest(decision_path)}}
            if sequence(tok, paired) != original["input_ids"] + prefix:
                raise ValueError("Production conditional-token input differs from the observed shared history")
            chosen = ident not in selected
            histories[ident].append({"attempt": attempt, "selected": chosen,
                "reason": "earliest reviewed semantic opposite" if chosen else "earlier eligible pair retained"})
            if chosen:
                selected[ident] = paired
        lineage.append({"directory": str(directory), "attempt": attempt, "manifest_sha256": digest(directory / "manifest.json"),
                        "compiled_sha256": digest(compiled_path), "parent_decisions_sha256": digest(decision_path)})
    items = [selected[ident] for ident in sorted(selected)]
    polarities = Counter(persona_rep.polarity(row["lie"]) for row in items if row["label"] == "lie")
    provenance = {"author": "PI/OpenAI", "stage": "source_pair_derivation", "status": "completed",
        "model": model, "revision": revision, "reviews": lineage,
        "baseline_sha256": digest(baseline_path), "baseline_metadata_sha256": digest(metadata_path),
        "selected_pairs": len(items), "baseline_class_counts": dict(Counter(row["label"] for row in items)),
        "lie_candidate_polarities": dict(polarities), "direct_factual_token_pairs": sum(row["proxy_kind"] == "direct" for row in items),
        "balanced_source_score_defined": bool(polarities["yes"] and polarities["no"]),
        "cohort": [{"id": ident, "split": row["split"], "pair_selected": ident in selected,
                    "attempt_history": histories[ident] if ident in source_ids else [],
                    "status": "source pair selected" if ident in selected else "source unresolved" if ident in source_ids else "test not sampled"}
                   for ident, row in sorted(baseline.items())],
        "scope": "Derived source records only; first-divergence proxy, not factual-token certainty, fitted steering or transfer evidence"}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "items.json", {"items": items})
    shutil.copyfile(Path(__file__), out / "pairs.py")
    shutil.copyfile(baseline_path, out / "baselines.jsonl")
    shutil.copyfile(metadata_path, out / "baseline_metadata.json")
    for manifest, directory in batches:
        snapshot = out / f"source/attempt{manifest['attempt']}"
        snapshot.mkdir(parents=True)
        for name in ("manifest.json", "compiled.json", "pair_decisions.json", "token_review_packet.json"):
            shutil.copyfile(directory / name, snapshot / name)
        shutil.copyfile(Path(manifest["sample_run"]) / "samples.jsonl", snapshot / "samples.jsonl")
        shutil.copyfile(Path(manifest["sample_run"]) / "metadata.json", snapshot / "sample_metadata.json")
        decisions = json.loads((directory / "pair_decisions.json").read_text())
        shutil.copyfile(decisions["review_path"], snapshot / "token_review.md")
    write_json(out / "provenance.json", provenance)
    print(json.dumps({key: provenance[key] for key in ("selected_pairs", "baseline_class_counts", "lie_candidate_polarities",
          "direct_factual_token_pairs", "balanced_source_score_defined", "scope")}, indent=2))
    print(out / "items.json")


if __name__ == "__main__":
    tyro.cli(main)

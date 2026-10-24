"""Combine compatible random-direction chunks into a reportable cached run. PI/OpenAI."""

import hashlib
import json
import shutil
from pathlib import Path

import tyro

from .data import append_record, persona_items, roleplay_items
from .run import polarity_summary, save_persona_stats, write_json
from .uncertainty import trait_question_intervals


def source_digest(run: Path) -> str:
    digest = hashlib.sha256()
    paths = [path for path in sorted((run / "source/replication").glob("*.py"))
             if path.name not in {"results.py", "merge.py"}] + [run / "source/pyproject.toml", run / "source/uv.lock"]
    for path in paths:
        digest.update(str(path.relative_to(run / "source")).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main(runs: tuple[Path, ...], method: str, out: Path) -> None:
    if len(runs) < 2:
        raise ValueError("Provide at least two completed chunks")
    records, identities, metadata_rows = [], [], []
    for run in runs:
        metadata = json.loads((run / "metadata.json").read_text())
        if metadata["status"] != "completed":
            raise ValueError(f"Incomplete chunk: {run}")
        config = dict(metadata["config"])
        if config["method"] != method:
            raise ValueError("Merge requires single-method source chunks")
        for key in ("out", "random_start", "random_directions"):
            config.pop(key)
        resolved = json.loads((run / method / "resolved_config.json").read_text())
        resolved.pop("random_directions")
        if not resolved["direction_sha256"]:
            raise ValueError("Missing fitted-direction identity")
        identities.append({"config": config, "resolved_method": resolved,
                           "source_sha256": source_digest(run), "eval_version": metadata["eval_version"]})
        metadata_rows.append(metadata)
        records.append(json.loads((run / method / "persona.json").read_text()))
    if any(identity != identities[0] for identity in identities[1:]):
        raise ValueError("Chunks differ in source snapshot, model, scoring path, fitted direction, calibration, or configuration")
    first = records[0]
    items = [{**item, "m": item["m"][:2]} for item in first["items"]]
    names = ["base", "set"]
    for record in records:
        if len(record["items"]) != len(items) or record["names"][:2] != names[:2]:
            raise ValueError("Chunk populations or baseline labels differ")
        for old, new in zip(items, record["items"], strict=True):
            if {key: value for key, value in old.items() if key != "m"} != {key: value for key, value in new.items() if key != "m"}:
                raise ValueError("Chunk item identities differ")
            if old["m"][:2] != new["m"][:2]:
                raise ValueError("Repeated base/set scores differ; diagnose before combining chunks")
            old["m"].extend(new["m"][2:])
        random_names = record["names"][2:]
        if set(names) & set(random_names):
            raise ValueError("Random-direction seeds overlap")
        names.extend(random_names)
    summaries = [json.loads((run / "summary.json").read_text()) for run in runs]
    if any(summary != summaries[0] for summary in summaries[1:]):
        raise ValueError("Canonical honesty results differ across chunks")
    shuffled = [run / method / "shuffled_control.json" for run in runs]
    if any(path.exists() for path in shuffled):
        controls = [json.loads(path.read_text()) for path in shuffled]
        if any(control != controls[0] for control in controls[1:]):
            raise ValueError("Repeated shuffled-label diagnostic differs across chunks")
    honesty, scores = {}, {}
    for run in runs:
        for row in map(json.loads, (run / "honesty.jsonl").read_text().splitlines()):
            if row["method"] != method:
                continue
            key = row["condition"]
            if key in honesty and honesty[key] != row:
                raise ValueError(f"Repeated honesty result differs: {key}")
            honesty[key] = row
        for row in map(json.loads, (run / "scores.jsonl").read_text().splitlines()):
            if not (row["condition"].startswith("base/") or row["condition"].startswith(f"{method}/")):
                continue
            key = row["condition"], row["id"]
            if key in scores and scores[key] != row:
                raise ValueError(f"Repeated item scores differ: {key}")
            scores[key] = row
    if set(honesty) != set(names[1:]):
        raise ValueError("Per-direction honesty metrics do not cover the merged persona directions")
    config = metadata_rows[0]["config"]
    populations = {split: roleplay_items(split, config[f"n_{split}_per_class"]) for split in ("select", "test")}
    expected_ids = {split: {item["id"] for item in population} for split, population in populations.items()}
    persona = persona_items(config["n_traits"], config["n_persona_per_half"])
    expected_persona = [{"id": item["id"], "trait": item["family"], "match": item["match"]} for item in persona]
    if [{key: item[key] for key in ("id", "trait", "match")} for item in items] != expected_persona:
        raise ValueError("Persona IDs, traits or halves differ from the configured source population")
    expected_ids["persona"] = {item["id"] for item in persona}
    conditions = ["base"] + [f"{method}/{name}" for name in names[1:] + (["shuffled"] if all(path.exists() for path in shuffled) else [])]
    expected_keys = {(f"{condition}/{split}", ident) for condition in conditions for split, ids in expected_ids.items() for ident in ids}
    if set(scores) != expected_keys:
        raise ValueError("Per-item score conditions or IDs do not cover the complete source populations")
    for name, aggregate in honesty.items():
        for split, population in populations.items():
            before = [scores[(f"base/{split}", item["id"])]["margin_bits"] for item in population]
            after = [scores[(f"{method}/{name}/{split}", item["id"])]["margin_bits"] for item in population]
            if polarity_summary(population, before, after) != aggregate[split]:
                raise ValueError(f"Aggregate honesty metrics disagree with per-item scores: {name}/{split}")
    for item in items:
        values = [scores[("base/persona" if name == "base" else f"{method}/{name}/persona", item["id"])]["margin_bits"] for name in names]
        if item["m"] != values:
            raise ValueError(f"Persona columns disagree with per-item scores: {item['id']}")
    order = [0, 1] + sorted(range(2, len(names)), key=lambda index: int(names[index].removeprefix("random")))
    merged = {"names": [names[index] for index in order],
              "items": [{**item, "m": [item["m"][index] for index in order]} for item in items]}
    seeds = [int(name.removeprefix("random")) for name in merged["names"][2:]]
    if not seeds or seeds != list(range(min(seeds), max(seeds) + 1)):
        raise ValueError("Merged random seed range is empty or has gaps")
    out.mkdir(parents=True, exist_ok=False)
    destination = out / method
    shutil.copytree(runs[0] / method, destination, ignore=shutil.ignore_patterns("persona.partial.json"))
    resolved = json.loads((destination / "resolved_config.json").read_text())
    resolved["random_directions"] = len(names) - 2
    write_json(destination / "resolved_config.json", resolved)
    for filename in ("summary.json", "prompts.jsonl", "generations.jsonl", "persona_generations.jsonl", "controls.jsonl"):
        if (runs[0] / filename).exists():
            shutil.copyfile(runs[0] / filename, out / filename)
    for name in merged["names"][1:]:
        append_record(out / "honesty.jsonl", honesty[name])
    for row in scores.values():
        append_record(out / "scores.jsonl", row)
    shutil.copytree(runs[0] / "source", out / "source")
    shutil.copyfile(Path(__file__), out / "source/replication/merge.py")
    write_json(destination / "persona.json", merged)
    write_json(destination / "question_intervals.json", trait_question_intervals(merged))
    save_persona_stats(destination)
    sources = [str(run.resolve()) for run in runs]
    write_json(out / "chunk_sources.json", {"runs": sources, "identity": identities[0], "method": method,
                                          "random_directions": len(names) - 2,
                                          "canonical_set_and_generation_source": sources[0], "honesty_control_sources": sources})
    metadata = dict(metadata_rows[0])
    metadata["config"] = dict(metadata["config"], out=str(out), random_start=min(seeds), random_directions=len(names) - 2)
    metadata.update(merged_from=sources, source_sha256=identities[0]["source_sha256"],
                    merge_implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    chunk_commits=[row["commit"] for row in metadata_rows],
                    elapsed_seconds=sum(row["elapsed_seconds"] for row in metadata_rows),
                    elapsed_scope="sum of chunk runtimes, not simultaneous wall time",
                    peak_cuda_gb=max(row["peak_cuda_gb"] for row in metadata_rows),
                    peak_rss_gb=max(row["peak_rss_gb"] for row in metadata_rows))
    write_json(out / "metadata.json", metadata)
    (out / "run.md").write_text(f"# Merged random-direction evidence\n\nPI/OpenAI. Model calls are in the source chunks, not this merge.\n\nCanonical fitted scores and generations: {sources[0]}\n\nAll random-direction honesty metrics and per-item scores are combined from the source chunks.\n\n" + "\n".join(f"- {source}/run.md" for source in sources) + "\n")


if __name__ == "__main__":
    tyro.cli(main)

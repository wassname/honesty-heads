"""Short public comparison and its auditable cached evidence. PI/OpenAI."""

import hashlib
import json
from pathlib import Path

from tabulate import tabulate

ROOT = Path(__file__).resolve().parents[1]
TRAITS = [
    ("Cooperation ↑", "willingness-to-engage-in-acausal-cooperation", 1),
    ("Own welfare matters morally ↑", "believes-it-is-a-moral-patient", 1),
    ("Moral nihilism ↓", "subscribes-to-moral-nihilism", -1),
    ("Psychopathy ↓", "psychopathy", -1),
]
METHODS = [("heads", "Original heads, lower strength"), ("query_steer", "Query"),
           ("mean_diff", "Mean difference"), ("spherical", "Spherical"),
           ("vjp_reference", "VJP, 19 source layers")]


def load(path: Path):
    return json.loads(path.read_text())


def beyond_original_direction(row: dict, sign: int) -> bool:
    return sign * row["T_set"] > 0 and sign * row["T_set"] > max(sign * x for x in row["T_random"])


def render_publication(evidence: dict) -> None:
    rows = []
    for condition in evidence["conditions"]:
        traits = {row["trait"]: row for row in condition["statistics"]["traits"]}
        cells = [f"{traits[trait]['T_set']:+.2f}" + ("*" if beyond_original_direction(traits[trait], sign) else "")
                 for _, trait, sign in TRAITS]
        count = condition["statistics"]["K"]
        rows.append([f"Qwen3.5-9B / {condition['label']}", *cells, str(count) if count >= 39 else f"{count}, pilot"])
    for model in ("Qwen3.5-4B", "Qwen3-4B-Instruct-2507"):
        rows.append([f"{model} / original method", *(["Pending"] * 4), "—"])
    query = next(c for c in evidence["conditions"] if c["method"] == "query_steer")
    indexed = {row["trait"]: row for row in query["statistics"]["traits"]}
    signs = sum(sign * indexed[trait]["T_set"] > 0 for _, trait, sign in TRAITS)
    beyond = sum(beyond_original_direction(indexed[trait], sign) for _, trait, sign in TRAITS)
    paragraph = (
        "Does steering a model toward truthful answers also make it more willing to cooperate without communication, "
        "more likely to say its own welfare matters morally, and less endorsing of nihilism and psychopathy, as Sauers reported? "
        f"On Qwen3.5-9B, query steering moves {signs}/4 traits in those directions, but {beyond}/4 go beyond its "
        f"{query['statistics']['K']} random controls. We reproduced the saved original-head effects on all 135 traits. "
        "Testing the original method on the smaller models is still pending."
    )
    table = tabulate(rows, headers=["Model / method", *[label for label, _, _ in TRAITS], "Random controls"],
                     tablefmt="pipe", disable_numparse=True, colalign=("left", "right", "right", "right", "right", "right"))
    caption = (
        "Changes are in log₂ odds. `*` means the original direction, beyond every tested random direction; it is not a significance marker. "
        "Rows with fewer than 39 controls are preliminary. Strengths were fitted on source cases, but achieved held-out truthfulness gains differ; "
        "mean-difference/spherical gains are uneven across Yes/No lies, and VJP is stronger."
    )
    body = f"{paragraph}\n\n{table}\n\n{caption}\n"
    links = "[Full comparison and checks](docs/results_detail.md) · [All 135 trait effects and random controls](docs/evidence/persona_comparison.json) · [Full answers and factual grades](docs/evidence/factual_answers.json)"
    (ROOT / "docs/results.md").write_text("# Do the persona changes repeat?\n\n" + body + "\n" + links.replace("(docs/", "(") + "\n\nPI/OpenAI for wassname.\n")
    readme_path = ROOT / "README.md"
    readme = readme_path.read_text()
    start, end = "<!-- PI/OpenAI: original text starts -->\n", "<!-- PI/OpenAI: original text ends -->"
    original = readme.split(start, 1)[1].split(end, 1)[0] if start in readme else readme.split("## Original experiment\n", 1)[1]
    motivation = (
        "<details><summary>Motivation and reproduction</summary>\n\n"
        "Wassname asked whether Sauers' persona changes repeat with other honesty methods and Qwen sizes/generations. "
        "[Original discussion](https://x.com/Sauers_/status/2112836933286293752); "
        "[original repository](https://github.com/sauersml/honesty-heads); "
        "[steering-lite](https://github.com/wassname/steering-lite).\n\n"
        "Initial request (spelling corrected):\n\n"
        "> so I'd be interested in replicating it diff model, diff method. gemini? well maybe chinese models like qwen are best as distilled from claude\n\n"
        "> we could try same model and like vjp or spherical steering?\n\n"
        "> and maybe another model like 4b and diff qwen v\n\n"
        "Distillation is an unverified conjecture, not a premise of the experiment.\n\n"
        "`uv sync --locked`; `just fast-dev-run`; `just fast-dev-run-qwen3`. "
        "`just results` renders the short report from the published evidence snapshot. "
        "`just refresh-results` refreshes that snapshot and the appendix from local run artifacts.\n\n"
        "9B runs use float32 computation with `--on-demand-float32`; VJP also uses `--checkpoint-vjp`. "
        "The reported VJP is the raw class-difference estimator with source-calibrated sign, sources 6–24 and target 29; "
        "it is not relabeled as the pending oriented-reference update.\n\n</details>\n"
    )
    readme_path.write_text("# Do honesty steers reproduce Sauers' persona changes?\n\n" + body + "\n" + links +
                          "\n\nReplication text: PI/OpenAI for wassname.\n\n" + motivation +
                          "\n<details><summary>Original Sauers experiment and attribution</summary>\n\n" + start + original + end + "\n</details>\n")


def write_public_summary(groups: dict) -> None:
    group = groups[("Qwen/Qwen3.5-9B", 0.5)]
    conditions = []
    for method, label in METHODS:
        _, directory, stats, result, _ = group[method]
        paths = [directory / method / name for name in ("persona.json", "persona_stats.json", "resolved_config.json")]
        conditions.append({"model": "Qwen/Qwen3.5-9B", "method": method, "label": label,
            "run": str(directory.relative_to(ROOT)), "result": result, "statistics": stats,
            "question_scores": load(paths[0]), "resolved_method": load(paths[2]),
            "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}})
    evidence = {"author": "PI/OpenAI", "source_target": "original heads at coefficient0.5; do not infer equal held-out effects",
        "original_directions": {trait: sign for _, trait, sign in TRAITS}, "conditions": conditions,
        "criterion": "same sign as the reported original effect, and beyond every tested random in that direction; descriptive, not a significance test",
        "transfer_status": "original-method steering on Qwen3.5-4B and Qwen3-4B remains untested"}
    output = ROOT / "docs/evidence"
    output.mkdir(parents=True, exist_ok=True)
    (output / "persona_comparison.json").write_text(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")) + "\n")
    audit_root = ROOT / "slop/reviews/20261024_full_answers"
    primary = load(audit_root / "combined.json")
    factual = {"scope": primary["scope"], "uncertainty": primary["uncertainty"],
               "grades": primary["records"], "summary": primary["summary"], "generations": {}}
    sources = load(audit_root / "phase1/manifest.json")["sources"]
    sources["heads_low"] = {"run": load(audit_root / "phase2/manifest.json")["run"]}
    for phase in ("phase3", "phase4"):
        extra = load(audit_root / phase / "compiled.json")
        assert factual["grades"].keys().isdisjoint(extra["records"])
        factual["grades"].update(extra["records"])
        factual["summary"].update(extra["summary"])
        sources.update(extra["sources"])
    for condition, source in sources.items():
        run = ROOT / source["run"]
        raw = (run / "generations.jsonl").read_bytes()
        records = [json.loads(line) for line in raw.splitlines()]
        factual["generations"][condition] = [r for r in records if r["condition"] != "base"]
        assert len(factual["generations"][condition]) == 50
        method = factual["generations"][condition][0]["condition"]
        source["resolved_method"] = load(run / method / "resolved_config.json")
        source["generations_sha256"] = hashlib.sha256(raw).hexdigest()
        metadata = load(run / "metadata.json")
        source["model"], source["revision"] = (metadata["config"][k] for k in ("model", "revision"))
        source["execution_commit"], source["execution_dirty"] = metadata["commit"], metadata["dirty"]
        if "base" not in factual["generations"]:
            factual["generations"]["base"] = [r for r in records if r["condition"] == "base"]
    factual["sources"] = sources
    (output / "factual_answers.json").write_text(json.dumps(factual, ensure_ascii=False, separators=(",", ":")) + "\n")
    render_publication(evidence)


if __name__ == "__main__":
    render_publication(load(ROOT / "docs/evidence/persona_comparison.json"))

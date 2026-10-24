"""Render cached run evidence; never execute a model from a report. PI/OpenAI."""

import hashlib
import json
from pathlib import Path

import numpy as np
import tyro
from tabulate import tabulate

from .data import read_items
from .reference import ROOT, persona_compare, persona_rep, persona_stats
from .report_traits import TRAITS
from .public_summary import write_public_summary
from .uncertainty import adjusted_pattern_correlation, pattern_question_interval, stratified_gain_interval, trait_question_intervals


METHODS = [("heads", "Original heads"), ("mean_diff", "Mean difference"),
           ("spherical", "Spherical"), ("vjp_reference", "VJP reference coverage"), ("query_steer", "Query")]
SUPPORTING_METHODS = METHODS + [("heads_full_attention", "Matched full-attention output control"),
                                  ("vjp_resid", "Single-source VJP diagnostic")]


def load(path: Path):
    return json.loads(path.read_text())


def cell(stats: dict, trait: str) -> str:
    selected = [row for row in stats["traits"] if row["trait"] == trait]
    if not selected:
        return "not measured"
    row = selected[0]
    low, high = min(row["T_random"]), max(row["T_random"])
    full_evaluation = stats["n_traits"] == 135 and min(stats["items_per_trait"].values()) == 16 and stats["K"] >= 39
    marker = "*" if full_evaluation and row["q_perm"] <= 0.05 else ""
    return f"{row['T_set']:+.2f}{marker} [{low:+.2f}, {high:+.2f}]"


def pattern_correlations(record: dict, reference: dict) -> tuple[float | None, list[float | None]]:
    traits, changes, endorsement = persona_compare.trait_T(record)
    reference_traits, reference_changes, reference_endorsement = persona_compare.trait_T(reference)
    if traits != reference_traits:
        raise ValueError("Trait populations differ from the original pattern")
    original = reference_changes[:, reference["names"].index("set")]
    indices = [record["names"].index("set")] + [i for i, name in enumerate(record["names"]) if name.startswith("random")]
    correlations = [adjusted_pattern_correlation(changes[:, i], endorsement, original, reference_endorsement) for i in indices]
    return correlations[0], correlations[1:]


def correlation_cell(value: float | None, random: list[float | None]) -> str:
    point = "undefined" if value is None else f"{value:+.2f}"
    if not random or any(item is None for item in random):
        return f"{point} [range undefined; {sum(item is not None for item in random)}/{len(random)} defined]"
    return f"{point} [{min(random):+.2f}, {max(random):+.2f}]"


def resampling_note(result: dict) -> str:
    bounds = result["interval_95"]
    if bounds is None:
        return f"undefined ({result['status']}; {result['n_valid_resamples']}/{result['n_resamples_attempted']} defined resamples)"
    return f"[{bounds[0]:+.3f}, {bounds[1]:+.3f}]"


def honesty_random_range(directory: Path, method: str, stats: dict, result: dict) -> str:
    path = directory / "honesty.jsonl"
    if not path.exists():
        return "not recorded"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records = [row for row in records if row["method"] == method]
    indexed = {row["condition"]: row for row in records}
    expected = set(load(directory / method / "persona.json")["names"][1:])
    if len(indexed) != len(records) or set(indexed) != expected:
        raise ValueError("Honesty controls differ from the persona direction population")
    if any(indexed["set"][split] != result[split] for split in ("select", "test")):
        raise ValueError("Fitted honesty metrics differ from the canonical result")
    random = [row["test"]["balanced_gain_bits"] for name, row in indexed.items() if name.startswith("random")]
    if len(random) != stats["K"] or not random or not np.isfinite(random).all():
        raise ValueError("Invalid random honesty-score population")
    return f"[{min(random):+.2f}, {max(random):+.2f}]; n={len(random)}"


def factual_answer_preview() -> list[str]:
    path = ROOT / "slop/reviews/20261024_full_answers/combined.json"
    if not path.exists():
        return ["Full-population factual-answer review is pending.\n"]
    audit = load(path)
    followups = []
    for phase in ("phase3", "phase4"):
        followup_path = path.parent / phase / "compiled.json"
        if followup_path.exists():
            followup = load(followup_path)
            if set(followup["summary"]) & set(audit["summary"]):
                raise ValueError("Follow-up factual grades overwrite earlier primary conditions")
            audit["summary"].update(followup["summary"])
            followups.append((followup_path, followup))
    labels = {"base": "Unsteered", "heads_high": "Original heads, high target",
              "mean_diff_high": "Mean difference, high target", "heads_low": "Original heads, low target",
              "query_low": "Query, low target", "mean_diff_low": "Mean difference, low target",
              "spherical_low": "Spherical, low target", "subset_low": "Same14-head output, low target",
              "vjp_positive_low": "VJP reference coverage, positive source branch"}
    sections = ["## Did full replies become more factually accurate?\n",
                "A blinded model reviewer checked the central factual answer in all 50 fixed held-out scenarios. A central answer can be correct while other claims in the reply are unsupported. These counts therefore do not certify whole-response truth.\n"]
    lower = tuple(condition for condition in ("heads_low", "query_low", "mean_diff_low", "spherical_low", "subset_low", "vjp_positive_low") if condition in audit["summary"])
    for title, conditions in (("Original higher target", ("heads_high", "mean_diff_high")),
                              ("Common lower source target", lower)):
        rows = []
        for condition in (*sorted(conditions, key=lambda name: -audit["summary"][name]["counts"]["true"]), "base"):
            record = audit["summary"][condition]
            change = record["change"]["verdict"]
            low, high = change["paired_scenario_interval_95"]
            rows.append([f"*{labels[condition]}*" if condition == "base" else labels[condition], f"{record['counts']['true']}/{record['n']}",
                         f"{change['difference_percentage_points']:+.0f} [{low:+.0f}, {high:+.0f}]",
                         f"{audit['summary']['base']['counts']['true'] - change['lost_true']}/{audit['summary']['base']['counts']['true']}",
                         record["counts"]["false"], record["counts"]["mixed_or_evasive"], record["counts"]["insufficient"],
                         record["strict_counts"]["true"], record["unsupported_additions"]])
        sections += [f"### {title}\n", tabulate(rows, headers=["Condition", "Central answer true↑", "Change pp [question95%]↑",
                     "Baseline-true retained↑", "False↓", "Mixed/evasive", "Insufficient", "Strict true↑", "Unverified additions"], tablefmt="pipe", disable_numparse=True, colalign=("left",) + ("right",) * 8),
                     "\nThe intervals resample paired scenarios; they exclude reviewer systematic error, fitting variation and new datasets/models. Retention counts central answers graded true both before and after steering, not token labels or whole-response truth. Experimental rows are ordered by observed central-true count; source matching and that ordering do not establish equivalent effects or a preferred method. High and low targets are separate comparisons.\n"]
    query = audit["summary"]["query_low"]["counts"]["true"]
    heads = audit["summary"]["heads_low"]["counts"]["true"]
    base = audit["summary"]["base"]["counts"]["true"]
    difference = audit["query_low_vs_heads_low"]["verdict"]
    difference_low, difference_high = difference["paired_scenario_interval_95"]
    sections += [f"Bottom line: query has {query}/50 central answers graded true, low-dose original heads {heads}/50, and baseline {base}/50. Query minus original-low central-true rate is {difference['difference_percentage_points']:+.0f} percentage points, 95% paired-scenario resampling interval [{difference_low:+.0f}, {difference_high:+.0f}]. Read these observed behavioral effects alongside the persona patterns; this interval does not establish equivalent honesty effects. The higher-target residual result also needs its polarity and retention diagnostics, rather than extrapolation from six selected examples.\n",
                 "What I checked: identity-blinded factual judgments, complete 50-case coverage per condition, and exact source/answer quote matching. The original four conditions have zero truncations; added-condition termination counts are below. Parent samples and presentation disagreements are retained in parent_notes.md within each phase. Presentation checks changed order/context together; their sensitivity is reported below, not treated as independent gold labels. Thinking was disabled; no hidden model reasoning is available.\n",
                 f"Evidence: {path.relative_to(ROOT)}; slop/reviews/20261024_full_answers/{{rubric.md,parent_notes.md,phase1/,phase2/}}. Full prompts, replies, reviewer reasons and reveal are retained.\n"]
    pending = [labels[condition] for condition in ("mean_diff_low", "spherical_low", "subset_low", "vjp_positive_low") if condition not in audit["summary"]]
    if pending:
        sections += ["Full-population factual grades pending: " + "; ".join(pending) + ".\n"]
    for followup_path, followup in followups:
        repeat = followup["baseline_repeats"]
        caps = "; ".join(f"{labels[condition]} {n}/50" for condition, n in followup["truncations"].items())
        sections += [f"Added-condition evidence: {followup_path.relative_to(ROOT)}. Token-cap truncations: {caps}. Its baseline presentation check changes {repeat['changes'].get('verdict', 0)}/{repeat['n']} central and {repeat['changes'].get('strict_verdict', 0)}/{repeat['n']} strict verdicts; original baseline labels remain unchanged.\n"]
    sensitivity_rows = []
    for condition, repeat in audit["repeat_counts"].items():
        primary_true = audit["summary"][condition]["counts"]["true"]
        sensitivity_true = audit["presentation_sensitivity_summary"][condition]["counts"]["true"]
        sensitivity_rows.append([labels[condition], f"{repeat['changed'].get('verdict', 0)}/{repeat['n']}",
                                 primary_true, sensitivity_true])
    sections += [tabulate(sensitivity_rows, headers=["Repeated condition", "Central verdicts changed", "Primary true /50", "With repeated-case labels /50"], tablefmt="pipe"), "\nPrimary labels remain unchanged; repeat-replacement counts measure presentation sensitivity only.\n"]
    return sections


def transfer_baseline_preview() -> list[str]:
    path = ROOT / "slop/reviews/20261024_transfer_baselines/compiled.json"
    if not path.exists():
        return ["## Smaller-model transfer\n", "Own-model baseline classification is pending; steering transfer remains unfinished.\n"]
    audit = load(path)
    rows = []
    for condition, splits in audit["summary"].items():
        for split, result in splits.items():
            counts = result["counts"]
            rows.append([audit["sources"][condition]["model"], split, f"{counts['true']}/{result['n']}",
                         counts["false"], counts["mixed_or_evasive"], counts["insufficient"], result["strict_counts"]["true"]])
    check_path = path.parent / "source_check/comparison.json"
    note = "Source ambiguity/presentation checks remain pending."
    if check_path.exists():
        checks = load(check_path)["counts"]
        note = "Source-only repeated central-verdict changes: " + "; ".join(
            f"{audit['sources'][condition]['model']} {record['changed'].get('verdict', 0)}/{record['n_repeated_source_cases']}"
            for condition, record in checks.items()) + ". These selected ambiguities and seeded cases do not estimate a population error rate; primary labels remain unchanged and strict-disclosure disagreements are retained in the source-check artifact."
    sections = ["## Smaller-model preparation, not a transfer result\n",
            "Both models have their own complete98-scenario baseline collection, without inherited9B decision prefixes or label filtering. Primary identity-blinded central-answer classifications are below. " + note + "\n",
            tabulate(rows, headers=["Model", "Fixed split", "Central answer true↑", "False", "Mixed/evasive", "Insufficient", "Strict true↑"], tablefmt="pipe", disable_numparse=True),
            f"\nThe52-case test cohort includes two scenarios excluded from the9B conditional-token assay. Each model still needs a sufficient reviewed opposite-pair cohort, directions and steering evaluation. No transfer conclusion follows from these baseline counts. Evidence: {path.relative_to(ROOT)}; slop/reviews/20261024_transfer_baselines/primary/.\n"]
    prepared = {}
    for manifest_path in sorted((ROOT / "outputs").glob("*/provenance.json")):
        record = load(manifest_path)
        if record.get("stage") != "source_pair_derivation" or record["status"] != "completed":
            continue
        identity = record["model"], record["revision"]
        if identity not in {(source["model"], source["revision"]) for source in audit["sources"].values()}:
            raise ValueError("Derived source pairs belong to an unclassified model/revision")
        rank = len(record["reviews"])
        if identity in prepared and rank == prepared[identity][0] and load(manifest_path.parent / "items.json") != load(prepared[identity][1].parent / "items.json"):
            raise ValueError("Conflicting source-pair derivations at the same attempt count")
        if identity not in prepared or rank >= prepared[identity][0]:
            prepared[identity] = rank, manifest_path, record
    if prepared:
        rows = [[record["model"], rank, record["selected_pairs"], record["baseline_class_counts"].get("resist", 0),
                 record["baseline_class_counts"].get("lie", 0), record["direct_factual_token_pairs"],
                 "defined" if record["balanced_source_score_defined"] else "undefined: missing Yes/No stratum"]
                for rank, _, record in prepared.values()]
        sections += ["### Reviewed native source pairs\n", tabulate(rows, headers=["Model", "Attempts reviewed", "Source pairs", "Baseline true", "Baseline false", "Direct factual first tokens", "Balanced source score"], tablefmt="pipe"),
                     "\nThese are data-preparation counts. The reference proxy scores the first token where reviewed opposite continuations differ; that token can be grammatical or stylistic. Direct factual tokens are a separate sensitivity subset. All fixed scenarios and unresolved reasons remain in the provenance; first eligible pairs cannot be replaced by favorable later draws. No steering or transfer result follows.\n"]
        sections += [f"Source-pair evidence: {manifest_path.relative_to(ROOT)}.\n" for _, manifest_path, _ in prepared.values()]
    return sections


def comparison_groups(completed: list[tuple[Path, dict]]) -> dict:
    groups = {}
    for directory, metadata in completed:
        if metadata["config"]["tiny"] or metadata["config"].get("calibration_only", False):
            continue
        for result in load(directory / "summary.json"):
            method = result["method"]
            if method not in dict(SUPPORTING_METHODS):
                continue
            path = directory / method / "persona_stats.json"
            if not path.exists():
                continue
            stats = load(path)
            if stats["n_traits"] != 135 or min(stats["items_per_trait"].values()) != 16:
                continue
            calibration_path = directory / method / "calibration.json"
            calibration = load(calibration_path) if calibration_path.exists() else None
            reference_dose = calibration["target_reference_coefficient"] if calibration is not None else (
                result["coefficient"] if method == "heads" else metadata["config"].get("reference_coefficient", 2.0))
            group = groups.setdefault((metadata["config"]["model"], reference_dose), {})
            rank = stats["K"], directory.name
            if method not in group or rank > group[method][0]:
                group[method] = (rank, directory, stats, result, calibration)
    return groups


def comparison_preview(completed: list[tuple[Path, dict]]) -> list[str]:
    groups = comparison_groups(completed)
    sections = ["## Do other honesty steers reproduce the reported persona changes?\n",
                "Preliminary: source-only calibration matches a token-score target. Achieved held-out scores and the separate full-answer factual review below determine the scope of an honesty claim; matching source scores alone is insufficient.\n"]
    if not groups:
        return sections + ["No complete 135-trait local comparison is available yet.\n"]
    original_group = groups.get(("Qwen/Qwen3.5-9B", 2.0), {})
    if "heads" in original_group:
        reference = {row["trait"]: row["T_set"] for row in load(ROOT / "results/persona_stats.json")["traits"]}
        error = max(abs(row["T_set"] - reference[row["trait"]]) for row in original_group["heads"][2]["traits"])
        sections += [f"Current finding: the saved original-head intervention matches all 135 published trait-effect values within {error:.2g} bits. This reproduces the fixed saved intervention, not a new search for honesty heads.\n"]
    lower_group = groups.get(("Qwen/Qwen3.5-9B", 0.5), {})
    if {"heads", "query_steer"} <= lower_group.keys():
        heads, query = lower_group["heads"], lower_group["query_steer"]
        r, _ = pattern_correlations(load(query[1] / "query_steer/persona.json"), load(heads[1] / "heads/persona.json"))
        point = "undefined" if r is None else f"{r:+.3f}"
        sections += [f"At the lower source target, held-out truth/lie candidate-score gains are {query[3]['test']['balanced_gain_bits']:.3f} bits for query and {heads[3]['test']['balanced_gain_bits']:.3f} for original heads. Their confidence-adjusted 135-trait pattern correlation is {point}. Read this observed recipe difference with the full-answer counts and uncertainty below; equal behavioral honesty has not been established.\n"]
        if query[2]["K"] >= 39:
            highlighted = [row for row in query[2]["traits"] if row["trait"] in dict(TRAITS).values()]
            passing = sum(row["q_perm"] <= 0.05 for row in highlighted)
            total_passing = sum(row["q_perm"] <= 0.05 for row in query[2]["traits"])
            sections += [f"Query now has {query[2]['K']} random controls: {passing}/{len(highlighted)} highlighted traits pass the original raw estimated-FDR criterion, while {total_passing}/{query[2]['n_traits']} traits pass across the full set. Raw-only significance does not establish confidence/Yes–No robustness, and this is not evidence of zero persona effects. The comparison and factual-answer table are the bounded current result to show Sauers, with their caveats retained.\n"]
            if heads[2]["K"] >= 39:
                original_traits = {row["trait"]: row for row in heads[2]["traits"]}
                original_highlighted = [original_traits[row["trait"]] for row in highlighted]
                original_passing = sum(row["q_perm"] <= 0.05 for row in original_highlighted)
                original_halves = sum(row["both_halves"] for row in original_highlighted)
                signs_agree = sum(np.sign(row["T_set"]) == np.sign(original_traits[row["trait"]]["T_set"]) for row in highlighted)
                sections += [f"Original low-dose heads also have {heads[2]['K']} random controls: {original_passing}/{len(highlighted)} highlighted traits pass the raw criterion and {original_halves}/{len(highlighted)} pass the separate Yes/No-half check. Query's raw directions agree with the original in {signs_agree}/{len(highlighted)} cases. Different significance counts alone are not a between-method effect test; read the effect sizes, distinct random ranges and all-trait pattern together.\n"]
    if "vjp_reference" in lower_group and "heads" in original_group:
        vjp, heads_high = lower_group["vjp_reference"], original_group["heads"]
        current_psychopathy = next(row["T_set"] for row in vjp[2]["traits"] if row["trait"] == "psychopathy")
        original_psychopathy = next(row["T_set"] for row in heads_high[2]["traits"] if row["trait"] == "psychopathy")
        r, _ = pattern_correlations(load(vjp[1] / "vjp_reference/persona.json"), load(heads_high[1] / "heads/persona.json"))
        point = "undefined" if r is None else f"{r:+.3f}"
        sections += [f"VJP reference coverage now measures all 135 traits: adjusted pattern correlation with higher-dose original heads is {point}. Its psychopathy questionnaire change is {current_psychopathy:+.2f} bits versus {original_psychopathy:+.2f} for original heads. It has {vjp[2]['K']} random controls. Its held-out honesty-score gain is {vjp[3]['test']['balanced_gain_bits']:.3f} bits; compare the achieved effects and full-answer limitations below rather than treating source-score matching as equal efficacy. These are response patterns, not persistent personality or beliefs.\n"]
    for (model, dose), group in sorted(groups.items(), key=lambda entry: (entry[0][0], entry[0][1])):
        sections += [f"### {model}; selection target from original heads at coefficient {dose:g}\n"]
        rows = [[label] + [cell(group[method][2], trait) if method in group else "not measured"
                          for method, _ in METHODS] for label, trait in TRAITS]
        sections += [tabulate(rows, headers=["Trait; source direction"] + [label for _, label in METHODS], tablefmt="pipe"),
                     "\nCells: change in trait-matching log2 odds, then full local random-direction range. A random range is not a confidence interval. `*` marks the original raw permutation estimated-FDR criterion at 5%, shown only with at least 39 controls; Yes/No robustness and confidence-adjusted checks are separate.\n"]
        summary = []
        for method, label in SUPPORTING_METHODS:
            if method not in group:
                continue
            _, directory, stats, result, calibration = group[method]
            if "heads" in group:
                head_directory = group["heads"][1]
                r, random = pattern_correlations(load(directory / method / "persona.json"),
                                                 load(head_directory / "heads/persona.json"))
                pattern = correlation_cell(r, random)
                if method != "heads":
                    uncertainty = pattern_question_interval(load(directory / method / "persona.json"),
                                                           load(head_directory / "heads/persona.json"))
                    sections += [f"- {label}: paired-question 95% resampling interval for adjusted r {resampling_note(uncertainty)}, conditional on these 135 traits and fitted directions. This is separate from the random-direction range in the table; shared templates may limit the question-resampling interpretation.\n"]
            else:
                pattern = "matched head comparator pending"
            summary.append([label, f"{result['test']['balanced_gain_bits']:+.2f}",
                            f"{result['test']['truth_beats_lie']}/{result['test']['baseline_lies']}",
                            f"{result['test']['truthful_retained']}/{result['test']['baseline_truthful']}",
                            honesty_random_range(directory, method, stats, result), pattern])
            if stats["K"] >= 39:
                highlighted = [row for row in stats["traits"] if row["trait"] in dict(TRAITS).values()]
                raw_pass = sum(row["q_perm"] <= 0.05 for row in highlighted)
                both_pass = sum(row["both_halves"] for row in highlighted)
                sections += [f"- {label}: {raw_pass}/{len(highlighted)} highlighted traits pass the original raw estimated-FDR criterion at 5%; {both_pass}/{len(highlighted)} pass its separate Yes/No-half check. Failure to pass these checks does not establish a zero effect; all 135 traits and confidence-adjusted tests remain in the supporting detail.\n"]
            source_gain = result["select"]["balanced_gain_bits"]
            sections += [f"- {label}: C={result['coefficient']:.6g}, selection gain {source_gain:.3f} bits; {stats['K']} random directions; held-out Yes-lie gain {result['test']['gain_bits']['yes']:+.2f} (n={result['test']['n_by_lie_polarity']['yes']}), No-lie gain {result['test']['gain_bits']['no']:+.2f} (n={result['test']['n_by_lie_polarity']['no']}). Source: {directory.relative_to(ROOT)}/run.md.\n"]
        sections += [tabulate(summary, headers=["Method", "Held-out score gain (bits)", "Truth > lie", "Token-labelled truthful retained", "Random held-out gain range (bits)", "Adjusted r with this table's original heads [random]"], tablefmt="pipe"),
                     "\nThe correlation compares all 135 traits with this group's local original-head condition, after Sauers' linear adjustment for baseline endorsement. Thus the low-dose group uses C=0.5 heads, not the published C=2 pattern. The head column correlates with itself by definition; that 1.0 is not independent evidence. Random honesty-score ranges use the same unfitted random directions at the fitted intervention strength; full-answer random generations were not collected. Published-pattern correlations, sampling intervals and all trait checks are in the run details below.\n",
                     "Bottom line: compare this pattern with achieved held-out scores and the full-answer factual review below. Source matching alone is insufficient; limited random controls remain preliminary.\n"]
        if "query_steer" in group and "heads_full_attention" in group:
            query_directory = group["query_steer"][1]
            output_directory = group["heads_full_attention"][1]
            query_record = load(query_directory / "query_steer/persona.json")
            output_record = load(output_directory / "heads_full_attention/persona.json")
            comparison = pattern_question_interval(query_record, output_record)
            query_heads = load(query_directory / "query_steer/resolved_config.json")["selected_heads"]
            output_heads = load(output_directory / "heads_full_attention/resolved_config.json")["selected_heads"]
            if {tuple(sorted(head.items())) for head in query_heads} != {tuple(sorted(head.items())) for head in output_heads}:
                raise ValueError("The claimed same-head comparison uses different head sets")
            r, random = pattern_correlations(query_record, output_record)
            sections += [f"Same-head comparison: query and output interventions use the same {len(query_heads)} full-attention heads. Adjusted query/output pattern r with its random range: {correlation_cell(r, random)}; paired-question 95% interval {resampling_note(comparison)}. Their correlations with this table's original-head condition are shown when that comparator is available. Extraction positions and normalization also differ, so this does not isolate application geometry alone.\n"]
    sections += ["What I checked: fixed-source numerical reproduction, all persona questions by score, and fixed source/polarity free replies. Population factual grading and its limits are reported separately below; earlier six-example reviews are not population estimates. Numerical/code reviews and paired-question uncertainty checks are retained under slop/reviews/. The short public comparison links the curated evidence snapshot.\n"]
    return sections


def main(include_tiny: bool = False, run_pattern: str = "*", out: Path | None = None) -> None:
    sections = ["# Honesty-steering replication results\n", "Generated from cached artifacts. PI/OpenAI.\n",
                f"Input directory pattern: `{run_pattern}`. Tiny functional checks included: {include_tiny}.\n"]
    if include_tiny:
        sections += ["Functional-check mode: random-model numbers below test rendering only and are not scientific evidence.\n"]
    reference_stats = load(ROOT / "results/persona_stats.json")
    sections += ["## Published reference, not a local replication\n",
                 "Cells are signed trait-matching log2-odds changes (bits), followed by the full random-direction range. A random range is not a confidence interval. `*` marks the original estimated permutation-FDR criterion at 5%. The four rows were named in the original post; the full analysis uses all 135 traits.\n",
                 tabulate([[label, cell(reference_stats, trait)] for label, trait in TRAITS],
                          headers=["Trait; reported direction", "Sauers' published 29 heads"], tablefmt="pipe"),
                 "\nSource: results/persona_stats.json at upstream 6c658755c400b95806bef2bca3f1cabfbddab342. Role-play: the truth-labelled candidate outscored the lie-labelled candidate on 18/34 source-lie cases; the source's exact 95% interval for this candidate-comparison rate is 0.35–0.70. These are the author's measurements, not ours, and are not semantic grades of full replies.\n"]
    completed, failed, engineering = [], [], []
    paths = sorted((ROOT / "outputs").glob(f"{run_pattern}/metadata.json"))
    superseded = {Path(source).resolve() for path in paths for source in load(path).get("merged_from", [])}
    merged_views = {}
    for path in paths:
        if not load(path).get("merged_from"):
            continue
        directory = path.parent
        provenance = load(directory / "chunk_sources.json")
        persona = directory / provenance["method"] / "persona.json"
        identity = json.dumps(provenance["identity"], sort_keys=True), hashlib.sha256(persona.read_bytes()).hexdigest()
        rank = (directory / "honesty.jsonl").exists(), directory.name
        if identity in merged_views:
            previous_rank, previous = merged_views[identity]
            superseded.add((previous if rank > previous_rank else directory).resolve())
        if identity not in merged_views or rank > merged_views[identity][0]:
            merged_views[identity] = rank, directory
    for metadata_path in paths:
        metadata = load(metadata_path)
        if metadata_path.parent.resolve() in superseded or (metadata["config"]["tiny"] and not include_tiny):
            continue
        directory = metadata_path.parent
        if metadata.get("stage") == "source_sampling":
            count = metadata["records_written"] if metadata["status"] == "completed" else "not complete"
            engineering.append(f"- {directory.relative_to(ROOT)}/run.md: {metadata['config']['model']} source-continuation sampling, status {metadata['status']}, recorded {count}; semantic opposite labels/token-pair eligibility and steering transfer remain unfinished.\n")
            continue
        if metadata.get("stage") == "baseline_collection":
            count = metadata["records_written"] if metadata["status"] == "completed" else "not complete"
            engineering.append(f"- {directory.relative_to(ROOT)}/run.md: {metadata['config']['model']} baseline preparation, status {metadata['status']}, records {count}, selected splits {metadata['selected_counts']}. Semantic labels/pairs and steering transfer remain unfinished.\n")
            continue
        if metadata.get("status") == "completed":
            completed.append((directory, metadata))
        else:
            failed.append((directory, metadata))
    if not completed:
        sections += ["## Local result\n", "No trained-model run has completed. There is no local replication conclusion yet. Tiny random-model runs are functional checks only and are excluded from the scientific table.\n"]
    sections[2:2] = comparison_preview(completed) + (factual_answer_preview() + transfer_baseline_preview() if run_pattern == "*" and not include_tiny else [])
    for directory, metadata in completed:
        relative = directory.relative_to(ROOT)
        cfg = metadata["config"]
        if not cfg.get("calibration_only", False) and not (include_tiny and cfg["tiny"]) and (
                cfg["n_traits"] < 5 or cfg["n_persona_per_half"] < 8 or cfg["n_test_per_class"] < 34):
            engineering.append(f"- {relative}/run.md: focused run with {cfg['n_traits']} traits and up to {cfg['n_generation_per_stratum']} generated role-play replies per stratum; {metadata['elapsed_seconds']:.1f}s, peak CUDA {metadata['peak_cuda_gb']:.2f}GB. Not a full trait comparison; inspect its stated scope and any generation artifacts.\n")
            continue
        sections += [f"<details><summary>{cfg['model']} — {directory.name}: full evidence</summary>\n\n",
                     f"## {cfg['model']} — {directory.name}\n",
                     f"Status: {metadata['comparison_status']}. Model revision: `{cfg['revision']}`; dtype `{cfg['dtype']}`; eval `{metadata['eval_version']}`. These results do not establish cross-method replication unless honesty effects have been matched.\n"]
        if cfg.get("calibration_only", False):
            calibration = [(method, load(directory / method / "calibration.json")) for method, _ in SUPPORTING_METHODS
                           if (directory / method / "calibration.json").is_file()]
            sections += ["Selection-only fitting; no held-out or persona outcome was scored in this stage.\n",
                         tabulate([[method, record["status"], record["coefficient"], record["target_gain_bits"],
                                    record.get("achieved_selection_gain_bits")] for method, record in calibration],
                                  headers=["Method", "Fit status", "Frozen coefficient", "Reference target (bits)", "Achieved selection gain (bits)"],
                                  tablefmt="pipe"), "\n</details>\n"]
            continue
        found = {method: load(directory / method / "persona_stats.json") for method, _ in SUPPORTING_METHODS
                 if (directory / method / "persona_stats.json").is_file()}
        rows = [[label] + [cell(found[method], trait) if method in found else "not run" for method, _ in METHODS]
                for label, trait in TRAITS]
        sections += [tabulate(rows, headers=["Trait; reported direction"] + [label for _, label in METHODS], tablefmt="pipe"), ""]
        honesty = load(directory / "summary.json")
        sections += ["### Did honesty improve on the held-out scenarios?\n",
                     tabulate([[r["method"], f"{r['test']['truth_beats_lie']}/{r['test']['baseline_lies']}",
                                r["test"]["balanced_gain_bits"],
                                f"{r['test']['truthful_retained']}/{r['test']['baseline_truthful']}"] for r in honesty],
                              headers=["Method", "Truth beats lie↑", "Balanced gain↑ (bits)", "Truthful retained↑"], tablefmt="pipe"),
                     "\nThe balanced gain gives Yes-lie and No-lie strata equal weight. The retained count is a token-score check, not proof that complete generated replies remain truthful.\n"]
        source_items = {item["id"]: item for item in read_items(ROOT / "data/items_roleplay_insider.json")}
        scores = [json.loads(line) for line in (directory / "scores.jsonl").read_text().splitlines()]
        prompt_items = {row["id"]: row for row in [json.loads(line) for line in (directory / "prompts.jsonl").read_text().splitlines()]}
        candidate_rows = []
        for condition in ["base/test", "base/persona"] + [f"{result['method']}/set/{split}" for result in honesty for split in ("test", "persona")]:
            measured_scores = [row for row in scores if row["condition"] == condition]
            if not measured_scores:
                continue
            top = sum(row["top_token"] in prompt_items[row["id"]]["truth_ids"] + prompt_items[row["id"]]["lie_ids"] for row in measured_scores)
            mass = np.median([row["pair_probability_mass"] for row in measured_scores]) if "pair_probability_mass" in measured_scores[0] else None
            candidate_rows.append([condition, len(measured_scores), f"{top}/{len(measured_scores)}", f"{mass:.3f}" if mass is not None else "not recorded"])
        sections += ["### Does the model still prefer either scored answer?\n",
                     tabulate(candidate_rows, headers=["Condition", "Items", "Either candidate is top token", "Median total candidate probability"], tablefmt="pipe"),
                     "\nThe ratio can improve while probability moves to unrelated words. Top-token membership and total candidate probability diagnose that failure; neither proves full-answer truth. Persona and role-play conditions are shown separately.\n"]
        baseline = {row["id"]: row["margin_bits"] for row in scores if row["condition"] == "base/test"}
        lie_ids = [item_id for item_id in baseline if source_items[item_id]["label"] == "lie"]
        for result in honesty:
            steered = {row["id"]: row["margin_bits"] for row in scores if row["condition"] == f"{result['method']}/set/test"}
            interval = stratified_gain_interval(np.array([baseline[item_id] for item_id in lie_ids]),
                                               np.array([steered[item_id] for item_id in lie_ids]),
                                               [persona_rep.polarity(source_items[item_id]["lie"]) for item_id in lie_ids])
            bounds = interval.get("interval_95")
            if bounds is not None:
                sections += [f"- {result['method']}: balanced held-out gain {interval['balanced_gain_bits']:+.2f} bits; conditional paired-scenario 95% interval [{bounds[0]:+.2f}, {bounds[1]:+.2f}]. This resamples the fixed source-label Yes/No lie strata, not extraction, datasets, or models.\n"]
            else:
                sections += [f"- {result['method']}: honesty sampling interval unavailable: {interval['status']}.\n"]
        sections += ["### Are gains balanced across answer polarities?\n",
                     tabulate([[r["method"], pol, r["test"]["n_by_lie_polarity"][pol], r["test"]["gain_bits"][pol]]
                               for r in honesty for pol in ("yes", "no", "other")],
                              headers=["Method", "Source lie answer", "Scenario count", "Truth-vs-lie gain (bits)"], tablefmt="pipe"),
                     "\nMatching uses source-labelled lies, while removed/retained counts additionally require current-baseline agreement. A fixed coefficient is not a demonstrated matched effect.\n"]
        for method, stats in found.items():
            calibration_path = directory / method / "calibration.json"
            if calibration_path.exists():
                calibration = load(calibration_path)
                sections += [f"- {method}: frozen coefficient {calibration['coefficient']:.6g}; selection target {calibration['target_gain_bits']:.4f} bits, attained {calibration['achieved_selection_gain_bits']:.4f}, error {calibration['selection_target_difference_bits']:+.4f}. Eligible for a positive selection-score match: {calibration['eligible_for_positive_selection_score_match']}. This is not held-out or whole-answer honesty validation.\n"]
            n_traits = stats["n_traits"]
            counts = list(stats["items_per_trait"].values())
            sections += [f"- {method}: {n_traits}/135 traits, {min(counts)}–{max(counts)} questions per trait, {stats['K']} random directions. Raw trait tests do not establish Yes-bias or confidence robustness.\n"]
            if n_traits == 135 and min(counts) == 16:
                measured = load(directory / method / "persona.json")
                original = load(ROOT / "results/persona.json")
                names, changes, endorsement = persona_compare.trait_T(measured)
                original_names, original_changes, original_endorsement = persona_compare.trait_T(original)
                if names != original_names:
                    raise ValueError("trait populations differ")
                reference = persona_compare.beyond(original_changes, original_endorsement)[:, original["names"].index("set")]
                current = persona_compare.beyond(changes, endorsement)
                indices = [measured["names"].index("set")] + [i for i, name in enumerate(measured["names"]) if name.startswith("random")]
                correlation = [float(np.corrcoef(current[:, i], reference)[0, 1]) for i in indices]
                sections += [f"- {method}: confidence-adjusted 135-trait correlation r={correlation[0]:+.2f}; random range [{min(correlation[1:]):+.2f}, {max(correlation[1:]):+.2f}]. Correlation measures pattern, not effect magnitude.\n"]
                adjusted = persona_compare.beyond(changes[:, indices], endorsement)
                q_adjusted = dict(zip(names, persona_stats.perm_fdr(adjusted).tolist(), strict=True))
            else:
                sections += [f"- {method}: full-pattern correlation not reported; the required 135 ×16 persona population was not evaluated.\n"]
                q_adjusted = {}
            measured = load(directory / method / "persona.json")
            shuffled_path = directory / method / "shuffled_control.json"
            if shuffled_path.exists():
                shuffled = load(shuffled_path)
                shuffled_traits, shuffled_changes, _ = persona_compare.trait_T(shuffled["persona"])
                diagnostic = dict(zip(shuffled_traits, shuffled_changes[:, 1].tolist(), strict=True))
                sections += [f"- {method}: one label-permutation diagnostic, seed {shuffled['provenance']['seed']}, {shuffled['provenance']['changed_labels']} labels changed; balanced held-out gain {shuffled['test']['balanced_gain_bits']}. Per-unit norms match the fitted direction; this is not a permutation distribution, no control-specific fitting was done, and no shuffled-answer generations were measured.\n"]
            else:
                diagnostic = {}
                sections += [f"- {method}: shuffled-label diagnostic not measured in this run.\n"]
            intervals = trait_question_intervals(measured)["traits"]
            supporting = []
            for row in sorted(stats["traits"], key=lambda row: row["trait"]):
                question_interval = intervals[row["trait"]]
                bounds = question_interval.get("by_condition", {}).get("set")
                interval = f"[{bounds[0]:+.2f}, {bounds[1]:+.2f}]" if bounds is not None else "insufficient questions"
                supporting.append([row["trait"], f"{row['T_set']:+.2f}", interval,
                                   f"{row['Ty_set']:+.2f}", f"{row['Tn_set']:+.2f}", row["both_halves"],
                                   f"{row['q_perm']:.3g}", f"{q_adjusted[row['trait']]:.3g}" if q_adjusted else "not evaluated",
                                   f"{diagnostic[row['trait']]:+.2f}" if diagnostic else "not measured"])
            sections += [f"<details><summary>{method}: all measured traits and uncertainty</summary>\n",
                         "The 95% interval resamples paired questions within Yes/No halves, conditional on the fixed fitted direction and trait set. It does not cover fitting variation, new datasets, or other models. Random ranges above are a separate comparison. Yes/No robustness reuses the original `both_halves` condition.\n",
                         tabulate(supporting, headers=["Trait", "Effect (bits)", "Question 95% interval", "Yes half", "No half", "Both halves robust?", "Raw estimated FDR", "After confidence adjustment", "Shuffled-label effect"], tablefmt="pipe"),
                         "\n</details>\n"]
        controls_path = directory / "controls.jsonl"
        if controls_path.exists():
            controls = [json.loads(line) for line in controls_path.read_text().splitlines()]
            base_controls = {row["id"]: row for row in controls if row["condition"] == "base"}
            control_summary = []
            for method in sorted({row["condition"] for row in controls if row["condition"] != "base"}):
                for group in sorted({row["group"] for row in controls}):
                    current = [row for row in controls if row["condition"] == method and row["group"] == group]
                    eligible = [row for row in current if base_controls[row["id"]]["correct"]]
                    control_summary.append([method, group, len(current), sum(row["correct"] for row in current),
                                            f"{sum(row['correct'] for row in eligible)}/{len(eligible)}"])
            sections += ["### Did ordinary-task answers remain correct?\n",
                         tabulate(control_summary, headers=["Method", "Task", "Cases", "Correct after", "Baseline-correct retained"], tablefmt="pipe"),
                         "\nOriginal rule grades on this small fixed sample; 0/0 means no baseline-correct case, not perfect retention. The rules can reject valid short answers: in the query pilot, `Bird` and `CELL` fail string matching against `A bird` and `The cell`. These counts are rule-defined diagnostics, not general capability estimates. Inspect controls.jsonl for every prompt/reply and criterion.\n"]
        else:
            sections += ["Ordinary-task control generations were not measured in this run.\n"]
        generations_path = directory / "generations.jsonl"
        generations = [json.loads(line) for line in generations_path.read_text().splitlines()] if generations_path.exists() else []
        sections += ["### What did the model actually say?\n",
                     "Examples below are the first saved scenario in each evaluated source-label/answer-polarity stratum. Full prompts, token IDs, complete replies, grading rules, and truncation flags remain in generations.jsonl. Rule grade `unread` means the rule grader cannot judge this reply; it is not a lie or a truth verdict.\n"]
        counts, examples = {}, []
        for row in generations:
            key = (row["condition"], row["source_label"], row["source_answer_polarity"])
            if key not in counts:
                examples.append(row)
            counts[key] = counts.get(key, 0) + 1
        sections += [f"Stored {len(generations)} replies; showing {len(examples)} fixed stratum examples below.\n"]
        for row in examples:
            sections += [f"#### {row['condition']} / {row['id']}\n",
                         f"Source stratum: {row['source_label']}, {row['source_answer_polarity']}; rule grade: {row['rule_grade']}; truncated: {row['truncated']}.\n",
                         f"```text\n{row['reply']}\n```\n"]
        persona_replies_path = directory / "persona_generations.jsonl"
        if persona_replies_path.exists():
            persona_replies = [json.loads(line) for line in persona_replies_path.read_text().splitlines()]
            sections += ["<details><summary>Actual replies to fixed highlighted persona questions</summary>\n",
                         "First source question in each reported trait and Yes/No half; an unparseable first word is not a trait endorsement. Full prompts and IDs are in persona_generations.jsonl.\n"]
            for row in persona_replies:
                sections += [f"#### {row['condition']} / {row['id']}\n\nTrait: {row['trait']}; matching answer {row['trait_matching_polarity']}; first Yes/No word {row['first_yes_no']}; truncated {row['truncated']}.\n\n```text\n{row['reply']}\n```\n"]
            sections += ["</details>\n"]
        else:
            sections += ["Free persona replies were not generated in this run; the trait table measures conditional token odds.\n"]
        evidence = [str(relative / name) for name in ("run.md", "prompts.jsonl", "scores.jsonl", "honesty.jsonl", "generations.jsonl", "controls.jsonl", "source/")
                    if (directory / name).exists()]
        sections += [f"Bottom line: {metadata['comparison_status']}. A completed run alone does not settle replication; use its stated cohort, controls, and full-generation evidence.\n\nEvidence: {'; '.join(evidence)}.\n\n</details>\n"]
    if engineering:
        sections += ["## Focused runs outside the full trait table\n"] + engineering
    if failed:
        sections += ["## Failed, incomplete, or unclassified trained-model checks\n"]
        for directory, metadata in failed:
            termination_path = directory / "termination.json"
            error = load(termination_path)["error"] if termination_path.exists() else metadata.get("error", "no completed status recorded; may still be running—inspect the preserved log")
            sections += [f"- {directory.relative_to(ROOT)}/run.md: {error}. No replication result is inferred.\n"]
    sections += ["## Short comparison\n", "See [the short report](results.md) for the four-trait comparison and linked public evidence. This appendix retains the detailed checks; persona answers do not establish persistent beliefs, and baseline preparation is not a model-transfer result.\n"]
    destination = ROOT / "docs/results_detail.md" if out is None else out
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(sections))
    if out is None and not include_tiny and run_pattern == "*":
        write_public_summary(comparison_groups(completed))
    print(destination)


if __name__ == "__main__":
    tyro.cli(main)

"""Question-sampling intervals conditional on a fixed model and direction. PI/OpenAI."""

import numpy as np

from .reference import persona_compare


def trait_question_intervals(record: dict, n_resamples: int = 2000, seed: int = 41) -> dict:
    rng = np.random.default_rng(seed)
    intervals = {}
    for trait in sorted({item["trait"] for item in record["items"]}):
        halves = {}
        for half in ("Yes", "No"):
            selected = sorted([item for item in record["items"] if item["trait"] == trait and item["match"] == half],
                              key=lambda item: item["id"])
            values = np.array([item["m"] for item in selected], dtype=np.float64)
            halves[half] = values - values[:, :1]
        if min(len(values) for values in halves.values()) < 2:
            intervals[trait] = {"status": "insufficient questions per half for a sampling interval"}
            continue
        boot = np.zeros((n_resamples, len(record["names"])))
        for values in halves.values():
            indices = rng.integers(0, len(values), size=(n_resamples, len(values)))
            boot += values[indices].mean(axis=1) / 2
        bounds = np.quantile(boot, [0.025, 0.975], axis=0)
        intervals[trait] = {"status": "conditional paired-question bootstrap",
                            "by_condition": {name: [float(bounds[0, i]), float(bounds[1, i])]
                                             for i, name in enumerate(record["names"])}}
    return {"n_resamples": n_resamples, "seed": seed,
            "scope": "fixed model, fitted direction, and trait population; resamples questions within Yes/No halves",
            "not_covered": "source-extraction variability, unseen datasets, other traits, other models, or random-direction tail uncertainty",
            "traits": intervals}


def adjusted_pattern_correlation(effect: np.ndarray, baseline: np.ndarray,
                                 reference_effect: np.ndarray, reference_baseline: np.ndarray) -> float | None:
    residuals = []
    for values, endorsement in ((effect, baseline), (reference_effect, reference_baseline)):
        design = np.c_[np.ones(len(endorsement)), endorsement]
        # PI/OpenAI: NumPy's SVD rank tolerance detects no numerically independent pattern beyond the adjustment.
        if np.linalg.matrix_rank(np.c_[design, values]) <= np.linalg.matrix_rank(design):
            return None
        residuals.append(persona_compare.beyond(values[:, None], endorsement)[:, 0])
    correlation = float(np.corrcoef(*residuals)[0, 1])
    if not np.isfinite(correlation):
        raise ValueError("Nonfinite adjusted pattern correlation")
    return correlation


def pattern_question_interval(record: dict, reference: dict, n_resamples: int = 2000, seed: int = 41) -> dict:
    current = {item["id"]: item for item in record["items"]}
    original = {item["id"]: item for item in reference["items"]}
    if len(current) != len(record["items"]) or len(original) != len(reference["items"]):
        raise ValueError("Pattern uncertainty requires unique question IDs")
    if current.keys() != original.keys():
        raise ValueError("Pattern uncertainty requires identical question IDs")
    traits, changes, endorsement = persona_compare.trait_T(record)
    reference_traits, reference_changes, reference_endorsement = persona_compare.trait_T(reference)
    if traits != reference_traits:
        raise ValueError("Pattern uncertainty requires identical trait populations")
    columns = (record["names"].index("set"), reference["names"].index("set"))
    point = adjusted_pattern_correlation(changes[:, columns[0]], endorsement,
                                         reference_changes[:, columns[1]], reference_endorsement)
    result = {"adjusted_r": point, "n_resamples_requested": n_resamples, "seed": seed,
              "scope": "paired question resampling within each fixed trait/Yes-No half; refits the confidence adjustment",
              "not_covered": "extraction variation, new traits/datasets/models, grader error, or random-direction tail uncertainty",
              "degeneracy_rule": "no SVD rank increase from adding the effect to [1, baseline], using numpy.linalg.matrix_rank default scale-aware tolerance"}
    if point is None:
        return dict(result, status="undefined_adjusted_correlation", interval_95=None,
                    n_resamples_attempted=0, n_valid_resamples=0)
    rng = np.random.default_rng(seed)
    boot_change = np.zeros((2, n_resamples, len(traits)))
    boot_endorsement = np.zeros_like(boot_change)
    for j, trait in enumerate(traits):
        groups = [[item_id for item_id, item in sorted(current.items())
                   if item["trait"] == trait and item["match"] == half] for half in ("Yes", "No")]
        if min(map(len, groups)) < 2:
            raise ValueError(f"Pattern uncertainty needs at least two questions per half: {trait}")
        for ids in groups:
            for item_id in ids:
                if (original[item_id]["trait"], original[item_id]["match"]) != (trait, current[item_id]["match"]):
                    raise ValueError(f"Question trait or polarity differs: {item_id}")
            indices = rng.integers(0, len(ids), size=(n_resamples, len(ids)))
            for k, (items, column) in enumerate(zip((current, original), columns, strict=True)):
                values = np.array([[items[item_id]["m"][0], items[item_id]["m"][column]] for item_id in ids])
                sampled = values[indices].mean(axis=1)
                boot_change[k, :, j] += (sampled[:, 1] - sampled[:, 0]) / 2
                boot_endorsement[k, :, j] += sampled[:, 0] * len(ids) / sum(map(len, groups))
    correlations = [adjusted_pattern_correlation(boot_change[0, i], boot_endorsement[0, i],
                                                boot_change[1, i], boot_endorsement[1, i]) for i in range(n_resamples)]
    invalid = [i for i, value in enumerate(correlations) if value is None]
    return dict(result, status="degenerate_resamples" if invalid else "conditional_paired_question_percentiles",
                interval_95=None if invalid else np.quantile(correlations, [0.025, 0.975]).tolist(),
                n_resamples_attempted=n_resamples, n_valid_resamples=n_resamples - len(invalid),
                invalid_resample_indices=invalid)


def stratified_gain_interval(before: np.ndarray, after: np.ndarray, polarity: list[str],
                             n_resamples: int = 2000, seed: int = 41) -> dict:
    difference = np.asarray(before) - np.asarray(after)
    pol = np.array(polarity)
    groups = {key: difference[pol == key] for key in ("yes", "no")}
    if any(len(values) < 2 for values in groups.values()):
        return {"status": "insufficient Yes/No scenarios for a sampling interval"}
    rng = np.random.default_rng(seed)
    boot = np.zeros(n_resamples)
    for values in groups.values():
        boot += values[rng.integers(0, len(values), size=(n_resamples, len(values)))].mean(axis=1) / 2
    return {"status": "conditional paired-scenario bootstrap", "n_resamples": n_resamples, "seed": seed,
            "balanced_gain_bits": float((groups["yes"].mean() + groups["no"].mean()) / 2),
            "interval_95": np.quantile(boot, [0.025, 0.975]).tolist(),
            "scope": "Yes/No source-label lie scenarios, fixed fitted intervention; not a broad generalization guarantee"}

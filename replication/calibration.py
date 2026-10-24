"""Selection-only coefficient fitting against the saved head intervention. PI/OpenAI."""

import hashlib
import json
from pathlib import Path

import numpy as np
import torch

from .data import sequence
from .methods import intervention
from .reference import ROOT, persona_rep


METRIC_VERSION = "source-lie-equal-yes-no-truth-margin-bits-v1"


def direction_digest(direction) -> str:
    digest = hashlib.sha256()
    if isinstance(direction, dict):
        digest.update(json.dumps(direction["units"], sort_keys=True).encode())
        trees = [direction["delta"]]
    else:
        digest.update(json.dumps(direction.cfg.to_dict(), sort_keys=True, default=str).encode())
        trees = [direction.shared, direction.stacked]
    for tree in trees:
        for key in sorted(tree, key=str):
            digest.update(str(key).encode())
            state = tree[key]
            values = state.values() if isinstance(state, dict) else state
            for value in values:
                tensor = value.detach().cpu().contiguous()
                digest.update(str((tensor.dtype, tuple(tensor.shape))).encode())
                digest.update(tensor.view(torch.uint8).numpy().tobytes())
    return digest.hexdigest()


def balanced_gain(items: list[dict], baseline: list[float], steered: list[float]) -> float:
    gains = np.array(baseline) - np.array(steered)
    indices = {pol: [i for i, item in enumerate(items) if item["label"] == "lie"
                     and persona_rep.polarity(item["lie"]) == pol] for pol in ("yes", "no")}
    if any(not values for values in indices.values()):
        raise ValueError("Coefficient matching needs source-label lies in both Yes and No strata")
    return float(sum(gains[values].mean() for values in indices.values()) / 2)


def calibration_identity(wrapped, cfg, items: list[dict], baseline: list[float], direction, reference) -> dict:
    records = [{"id": item["id"], "input_ids": sequence(wrapped.tok, item),
                "truth_ids": item["truth_ids"], "lie_ids": item["lie_ids"], "label": item["label"],
                "lie_polarity": persona_rep.polarity(item["lie"])} for item in items]
    implementation = hashlib.sha256()
    for path in (Path(__file__), ROOT / "replication/data.py", ROOT / "replication/methods.py", ROOT / "code/persona_rep.py"):
        implementation.update(path.read_bytes())
    return {"model": cfg.model, "revision": cfg.revision, "dtype": cfg.dtype,
            "attention_backend": cfg.attention_backend, "cpu_tail_layers": cfg.cpu_tail_layers,
            "on_demand_float32": cfg.on_demand_float32, "batch_size": cfg.batch_size,
            "tiny": cfg.tiny, "seed": cfg.seed, "source_records": records, "baseline_margins": baseline,
            "direction_sha256": direction_digest(direction), "reference_direction_sha256": direction_digest(reference),
            "reference_coefficient": cfg.reference_coefficient, "metric_version": METRIC_VERSION,
            "implementation_sha256": implementation.hexdigest(),
            "fit_policy": {"tolerance_bits": cfg.calibration_tolerance_bits, "max_refinements": cfg.calibration_refinements}}



def fit_coefficient(wrapped, cfg, items, baseline, direction, reference, score) -> dict:
    grid = sorted(set(cfg.coefficient_grid))
    if 0.0 not in grid or len(grid) < 2:
        raise ValueError("Prespecify zero and at least one nonzero coefficient")
    with intervention(wrapped, reference, cfg.reference_coefficient):
        target = balanced_gain(items, baseline, score("reference"))
    points = []
    for coefficient in grid:
        if coefficient == 0:
            values = baseline
        else:
            with intervention(wrapped, direction, coefficient):
                values = score(f"grid/{coefficient:g}")
        points.append({"coefficient": coefficient, "balanced_gain_bits": balanced_gain(items, baseline, values)})
    brackets = [(a, b) for a, b in zip(points[:-1], points[1:], strict=True)
                if min(a["balanced_gain_bits"], b["balanced_gain_bits"]) <= target
                <= max(a["balanced_gain_bits"], b["balanced_gain_bits"])
                and a["balanced_gain_bits"] != b["balanced_gain_bits"]]
    result = {"identity": calibration_identity(wrapped, cfg, items, baseline, direction, reference),
              "metric": METRIC_VERSION, "target_reference_coefficient": cfg.reference_coefficient,
              "target_gain_bits": target, "curve": points,
              "rule": "smallest-absolute-strength bracket; interpolate then bisect on selection only",
              "tolerance_basis": "Default 0.002 bits is numerical root-fitting precision, consistent with the existing reference agreement bound; the configured value is in identity.fit_policy, not an honesty-efficacy threshold",
              "scope": "in-sample source selection only; no test or persona outcomes used"}
    if not brackets:
        return dict(result, status="no_overlap_on_prespecified_grid", coefficient=None,
                    eligible_for_positive_selection_score_match=False)
    a, b = min(brackets, key=lambda pair: max(abs(pair[0]["coefficient"]), abs(pair[1]["coefficient"])))
    bracket = [dict(a), dict(b)]
    coefficient = a["coefficient"] + (target - a["balanced_gain_bits"]) * (
        b["coefficient"] - a["coefficient"]) / (b["balanced_gain_bits"] - a["balanced_gain_bits"])
    refinements = []
    best = min((a, b), key=lambda point: abs(point["balanced_gain_bits"] - target))
    for iteration in range(cfg.calibration_refinements + 1):
        with intervention(wrapped, direction, coefficient):
            achieved = balanced_gain(items, baseline, score(f"refine/{iteration}"))
        point = {"coefficient": coefficient, "balanced_gain_bits": achieved}
        refinements.append(point)
        if abs(achieved - target) <= abs(best["balanced_gain_bits"] - target):
            best = point
        if abs(best["balanced_gain_bits"] - target) <= cfg.calibration_tolerance_bits:
            break
        if (a["balanced_gain_bits"] - target) * (achieved - target) <= 0:
            b = point
        else:
            a = point
        coefficient = (a["coefficient"] + b["coefficient"]) / 2
    difference = best["balanced_gain_bits"] - target
    eligible = target > 0 and abs(difference) <= cfg.calibration_tolerance_bits
    return dict(result, status="frozen", coefficient=best["coefficient"],
                achieved_selection_gain_bits=best["balanced_gain_bits"], selection_target_difference_bits=difference,
                eligible_for_positive_selection_score_match=eligible, refinements=refinements, bracket=bracket,
                matching_scope="selection score only; held-out honesty, polarity and generation evidence still required")


def frozen_coefficient(record: dict, identity: dict) -> float:
    if record["status"] != "frozen":
        raise ValueError("Calibration did not bracket the reference effect; no matched comparison is defined")
    if record["identity"] != identity:
        raise ValueError("Frozen calibration identity differs from this model/source/direction/numerical path")
    return record["coefficient"]

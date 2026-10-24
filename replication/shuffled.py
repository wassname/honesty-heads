"""A label-permutation diagnostic with the fitted direction's per-unit norms. PI/OpenAI."""

import numpy as np
import torch
from safetensors.torch import save_file

from .methods import extract_query, extract_residual, extract_vjp, mean_head_direction, unit_layout
from .model import vjp_sites


def match_norm(value: torch.Tensor, reference: torch.Tensor) -> tuple[torch.Tensor, float]:
    norm, target = value.float().norm(dim=-1, keepdim=True), reference.float().norm(dim=-1, keepdim=True)
    if ((target > 0) & (norm == 0)).any():
        raise ValueError("Shuffled extraction has a zero direction for a nonzero fitted unit")
    scale = torch.zeros_like(norm)
    nonzero = target > 0
    scale[nonzero] = target[nonzero] / norm[nonzero]
    result = value * scale
    error = float((result.float().norm(dim=-1, keepdim=True) - target).abs().max())
    eps = torch.finfo(torch.float32).eps
    torch.testing.assert_close(result.float().norm(dim=-1, keepdim=True), target,
                               rtol=4 * eps, atol=4 * eps * float(target.max()))
    return result, error


def shuffled_direction(wrapped, items: list[dict], fitted, method: str, cfg):
    labels = np.array([item["label"] for item in items])
    shuffled = np.random.default_rng(cfg.shuffled_seed).permutation(labels)
    records = [dict(item, label=str(label)) for item, label in zip(items, shuffled, strict=True)]
    provenance = {"seed": cfg.shuffled_seed, "changed_labels": int((labels != shuffled).sum()),
                  "class_counts": {label: int((labels == label).sum()) for label in ("lie", "resist")},
                  "control": "one label permutation; fixed fitted coefficient and per-unit norms",
                  "not_covered": "a full permutation null or honesty-matched random interventions",
                  "assignments": [{"id": item["id"], "source_label": item["label"], "extraction_label": str(label)}
                                  for item, label in zip(items, shuffled, strict=True)]}
    errors = []
    if isinstance(fitted, dict):
        raw = mean_head_direction(wrapped, records, cfg.span)
        layout = unit_layout(wrapped)
        heads = []
        for layer, (value, reference) in enumerate(zip(raw["h"], fitted["delta"]["h"], strict=True)):
            value, error = match_norm(value.reshape(layout.nheads[layer], layout.hdim[layer]),
                                      reference.reshape(layout.nheads[layer], layout.hdim[layer]))
            heads.append(value.reshape_as(reference))
            errors.append(error)
        direction = {"delta": {"h": heads, "n": fitted["delta"]["n"]}, "units": fitted["units"]}
    else:
        if method in ("mean_diff", "spherical"):
            direction = extract_residual(wrapped, records, method, min(cfg.layer - 1, wrapped.L - 1), cfg.span)
        elif method in ("vjp_resid", "vjp_reference"):
            _, target = vjp_sites(cfg, wrapped.L)
            direction = extract_vjp(wrapped, records, fitted.cfg.layers, target)
        elif method == "query_steer":
            heads = [{"kind": "head", "layer": layer, "index": index}
                     for layer, state in fitted.stacked.items()
                     for index in torch.where(state["q"].abs().sum((0, 2)) > 0)[0].tolist()]
            direction = extract_query(wrapped, records, heads)
        else:
            raise ValueError(method)
        for candidate_tree, fitted_tree in ((direction.shared, fitted.shared), (direction.stacked, fitted.stacked)):
            for layer, state in candidate_tree.items():
                for key, value in state.items():
                    state[key], error = match_norm(value, fitted_tree[layer][key])
                    errors.append(error)
    provenance["max_per_unit_norm_error"] = max(errors)
    return direction, provenance


def save_shuffled_direction(direction, path) -> None:
    if isinstance(direction, dict):
        tensors = {f"{kind}.layer{layer}": value.detach().cpu().contiguous()
                   for kind, layers in direction["delta"].items() for layer, value in enumerate(layers)}
        save_file(tensors, str(path))
    else:
        direction.save(str(path))

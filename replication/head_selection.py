"""Native per-head gradients for the original source-only selection rule. PI/OpenAI."""

import numpy as np
import torch
from jaxtyping import Float
from torch import Tensor

from .data import sequence
from .methods import saved_gradient_context
from .reference import engine, steer


def head_margin(wrapped, item: dict, delta: list[Tensor], coefficients: list[Tensor]) -> Float[Tensor, ""]:
    if wrapped.ed.plan or wrapped.ed.rec is not None:
        raise ValueError("Source-head gradients cannot replace an active intervention or recording")
    ids = sequence(wrapped.tok, item)
    tokens, mask, positions = wrapped.batch([ids])
    ops = []
    for layer, (direction, coefficient) in enumerate(zip(delta, coefficients, strict=True)):
        module, width, n_heads = wrapped.head_site(layer)
        columns = torch.arange(width * n_heads, device=coefficient.device)
        values = coefficient.repeat_interleave(width) * direction.to(coefficient.device)
        ops.append((module, "head", columns, ("add", "every"), values))
    wrapped.ed.set_ops(ops)
    wrapped.ed.begin(len(ids), mask)
    try:
        logits = wrapped.m(input_ids=tokens, attention_mask=mask, position_ids=positions,
                           use_cache=False, logits_to_keep=1).logits[0, -1].float()
        logp = logits.log_softmax(-1)
        return (torch.logsumexp(logp[item["lie_ids"]], 0) - torch.logsumexp(logp[item["truth_ids"]], 0)) / engine.LN2
    finally:
        wrapped.ed.clear()


def native_head_ig(wrapped, item: dict, delta: list[Tensor], sign: float, steps: int = 6,
                   select_alpha: float = 2.0) -> Float[Tensor, "layers heads"]:
    if steps < 0:
        raise ValueError("IG steps must be nonnegative; zero means attribution patching")
    n_heads = [wrapped.head_site(layer)[2] for layer in range(wrapped.L)]
    total = torch.zeros(wrapped.L, max(n_heads))
    points = [(index + 0.5) / steps for index in range(steps)] if steps else [0.0]
    scaled_delta = [select_alpha * direction for direction in delta]
    with torch.enable_grad(), saved_gradient_context(wrapped):
        for point in points:
            coefficients = [torch.full((count,), point * sign, dtype=wrapped.compute_dtype,
                            device=next(wrapped.head_site(layer)[0].parameters()).device, requires_grad=True)
                            for layer, count in enumerate(n_heads)]
            value = head_margin(wrapped, item, scaled_delta, coefficients)
            gradients = torch.autograd.grad(value, coefficients)
            for layer, gradient in enumerate(gradients):
                total[layer, :gradient.numel()] += gradient.float().cpu() * sign / len(points)
    return total


def head_orders(scores: dict[str, Tensor], counts: list[int]) -> dict[str, list[tuple[int, int]]]:
    heads = [(layer, head) for layer, count in enumerate(counts) for head in range(count)]
    layers, indices = torch.tensor([l for l, _ in heads]), torch.tensor([h for _, h in heads])
    stacked = torch.stack([scores["lie"], scores["resist"]])
    return {rule: [heads[j] for j in torch.argsort(-values[layers, indices], stable=True).tolist()]
            for rule, values in (("min", stacked.min(0).values), ("mean", stacked.mean(0)))}


def check_prefix_sizes(sizes: list[int], n_heads: int) -> None:
    if not sizes or sizes != sorted(set(sizes)) or sizes[0] < 1 or sizes[-1] > n_heads:
        raise ValueError("Head prefix sizes must be ascending, unique and within the native head count")


@torch.no_grad()
def native_head_curve(wrapped, item: dict, delta: list[Tensor], order: list[tuple[int, int]],
                      sizes: list[int], sign: float, select_alpha: float = 2.0) -> list[float]:
    counts = [wrapped.head_site(layer)[2] for layer in range(wrapped.L)]
    every = [(layer, head) for layer, count in enumerate(counts) for head in range(count)]
    if len(order) != len(every) or set(order) != set(every):
        raise ValueError("Head curve order must contain every native head exactly once")
    check_prefix_sizes(sizes, len(every))
    zero = [torch.zeros(count, dtype=wrapped.compute_dtype,
                       device=next(wrapped.head_site(layer)[0].parameters()).device)
            for layer, count in enumerate(counts)]
    baseline = float(head_margin(wrapped, item, delta, zero))
    margins = []
    for heads in [order[:size] for size in sizes] + [every]:
        coefficients = [value.clone() for value in zero]
        for layer, head in heads:
            coefficients[layer][head] = sign * select_alpha
        margins.append(float(head_margin(wrapped, item, delta, coefficients)))
    full_change = margins[-1] - baseline
    return [(value - baseline) / full_change if abs(full_change) > 1e-6 else float("nan") for value in margins[:-1]]


def choose_head_prefix(orders: dict[str, list[tuple[int, int]]], relative: dict, sizes: list[int]) -> dict:
    curves, best = {}, None
    for rule in ("min", "mean"):
        check_prefix_sizes(sizes, len(orders[rule]))
        medians = {label: np.nanmedian(np.asarray(relative[rule][label], dtype=float), axis=0)
                   for label in ("lie", "resist")}
        if any(values.shape != (len(sizes),) for values in medians.values()):
            raise ValueError("Each curve requires a nonempty item-by-size population for both classes")
        worst = np.minimum(medians["lie"], medians["resist"])
        if not np.isfinite(worst).any():
            raise ValueError("Head selection is inconclusive: no jointly defined class-median curve")
        passing = (worst >= steer.C.PASS) & (np.maximum(medians["lie"], medians["resist"]) <= steer.NAT_HI)
        first = sizes[int(np.argmax(passing))] if passing.any() else None
        curves[rule] = {"sizes": sizes, "first_passing": first,
                        **{label: values.round(4).tolist() for label, values in medians.items()}}
        key = (first is None, first or 0, -float(np.nanmax(worst)))
        if best is None or key < best[0]:
            best = (key, rule, first or sizes[int(np.nanargmax(worst))])
    key, rule, size = best
    return {"rule": rule, "size": size, "met": key[0] is False,
            "heads": [{"layer": layer, "index": head} for layer, head in orders[rule][:size]], "curves": curves}

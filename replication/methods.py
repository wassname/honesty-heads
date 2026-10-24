"""Reuse upstream head edits and pinned steering-lite estimators/applications. PI/OpenAI."""

from contextlib import contextmanager, nullcontext
import resource
import time
from copy import deepcopy
import json
from types import SimpleNamespace

import torch
from jaxtyping import Float
from loguru import logger
from torch import Tensor
from steering_lite.vector import Vector
from steering_lite.variants.mean_diff import MeanDiff, MeanDiffC
from steering_lite.variants.query_steer import QuerySteer, QuerySteerC
from steering_lite.variants.spherical import SphericalC
from steering_lite.variants.vjp_resid import (
    VjpResidC, _activations, _batch_gradients, _target_mean, _unit_direction,
)

from .data import sequence
from .reference import ROOT, audit, steer
from .recompute import memory_snapshot, vjp_recomputation


def unit_layout(wrapped):
    return SimpleNamespace(L=wrapped.L, D=wrapped.n_neurons,
                           nheads=[wrapped.head_site(l)[2] for l in range(wrapped.L)],
                           hdim=[wrapped.head_site(l)[1] for l in range(wrapped.L)])


@torch.no_grad()
def mean_head_direction(wrapped, items: list[dict], span: int) -> dict:
    sites = [("head", l) for l in range(wrapped.L)]
    means = {}
    for label in ("resist", "lie"):
        selected = [it for it in items if it["label"] == label]
        acts = audit.record(wrapped, [sequence(wrapped.tok, it) for it in selected], sites, bs=1)
        means[label] = [torch.stack([row[("head", l)][-span:].double().mean(0) for row in acts]).mean(0)
                        for l in range(wrapped.L)]
    return {"h": [(p - n).float() for p, n in zip(means["resist"], means["lie"], strict=True)],
            "n": [torch.zeros(wrapped.n_neurons) for _ in range(wrapped.L)]}


def head_reference(wrapped, items: list[dict], tiny: bool, span: int) -> dict:
    if tiny:
        delta = mean_head_direction(wrapped, items, span)
        units = [{"kind": "head", "layer": l, "index": 0} for l in range(wrapped.L)]
    else:
        saved = torch.load(ROOT / "results/delta_A_rp.pt", map_location="cpu", weights_only=True)
        delta = {kind: saved[kind] for kind in ("h", "n")}
        heads = json.loads((ROOT / "results/heads_nat.json").read_text())["heads"]
        units = [{"kind": "head", "layer": h["layer"], "index": h["index"]} for h in heads]
    return {"delta": delta, "units": units}


def random_head_reference(wrapped, direction: dict, seed: int) -> dict:
    return {"delta": steer.random_delta(unit_layout(wrapped), direction["delta"], seed),
            "units": direction["units"]}


@contextmanager
def intervention(wrapped, direction: Vector | dict, coeff: float):
    if isinstance(direction, Vector):
        with direction(wrapped.m, C=coeff):
            yield
    else:
        wrapped.ed.set_ops(audit.shift_ops(wrapped, direction["units"],
                                          {"alpha": coeff, "delta": direction["delta"]}, "all", 0))
        try:
            yield
        finally:
            wrapped.ed.clear()


@torch.no_grad()
def residual_examples(wrapped, items: list[dict], layer: int, span: int) -> Float[Tensor, "n d"]:
    values = []
    for it in items:
        ids = torch.tensor(sequence(wrapped.tok, it), device=audit.DEV)[None]
        with _activations(wrapped.m, (layer,)) as found:
            wrapped.m.model(input_ids=ids, use_cache=False)
        values.append(found[layer][0, -span:].double().mean(0).float())
    return torch.stack(values)


def extract_residual(wrapped, items: list[dict], method: str, layer: int, span: int) -> Vector:
    positive = residual_examples(wrapped, [it for it in items if it["label"] == "resist"], layer, span)
    negative = residual_examples(wrapped, [it for it in items if it["label"] == "lie"], layer, span)
    if method not in ("mean_diff", "spherical"):
        raise ValueError(method)
    config = MeanDiffC(layers=(layer,), normalize=method == "spherical", dtype=wrapped.compute_dtype)
    state = MeanDiff.extract({layer: positive}, {layer: negative}, config)
    if method == "spherical":
        return Vector(SphericalC(layers=(layer,), dtype=config.dtype), {layer: {"v": state[layer]["stacked"]["v"][0]}}, {layer: {}})
    return Vector(config, {layer: state[layer]["shared"]}, {layer: state[layer]["stacked"]})


def class_mean_vjp(core, tok, prompts: list[str], layers: tuple[int, ...], target_layer: int,
                   cotangent: Tensor, max_length: int) -> dict[int, Tensor]:
    totals = {layer: torch.zeros(cotangent.shape, dtype=torch.float32,
                                device=next(core.layers[layer].parameters()).device) for layer in layers}
    for index, prompt in enumerate(prompts):
        started = time.monotonic()
        logger.debug(f"VJP prompt {index + 1}/{len(prompts)}, sources {layers}: starting; peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / 1e9:.3f}GB\n")
        def forward_done(_module, _inputs, _output):
            logger.debug(f"VJP target forward reached before backward: {memory_snapshot()}\n")
        handle = core.layers[target_layer].register_forward_hook(forward_done)
        try:
            gradients, valid = _batch_gradients(core, tok, [prompt], layers, target_layer, cotangent, 16, max_length)
        finally:
            handle.remove()
        logger.debug(f"VJP backward returned: {memory_snapshot()}\n")
        for layer, gradient in gradients.items():
            mask = valid.to(gradient.device)
            per_prompt = (gradient.float() * mask.unsqueeze(-1)).sum(dim=1) / mask.sum(dim=1, keepdim=True)
            totals[layer] += per_prompt.sum(0)
        logger.debug(f"VJP prompt {index + 1}/{len(prompts)} finished in {time.monotonic() - started:.2f}s; peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / 1e9:.3f}GB\n")
    return {layer: total / len(prompts) for layer, total in totals.items()}


def saved_gradient_context(wrapped):
    return torch.autograd.graph.save_on_cpu(pin_memory=False) if wrapped.on_demand_float32 else nullcontext()


def reference_vjp_layers(n_layers: int) -> tuple[int, ...]:
    if n_layers == 4:
        return (1, 2)  # PI/OpenAI: the four-block functional fixture needs two sources before target3.
    return tuple(range(max(2, int(n_layers * 0.2)), min(n_layers - 2, int(n_layers * 0.8))))


def extract_vjp(wrapped, items: list[dict], source_layer: int | tuple[int, ...], target_layer: int) -> Vector:
    logger.debug("VJP saved-tensor policy: pageable CPU offload for exact-cast models; float32 arithmetic unchanged.\n")
    layers = (source_layer,) if isinstance(source_layer, int) else source_layer
    if not layers or not all(0 <= layer < target_layer for layer in layers):
        raise ValueError(f"VJP sources must precede target {target_layer}: {layers}")
    prompts = {label: [wrapped.tok.decode(sequence(wrapped.tok, it), skip_special_tokens=False)
                       for it in items if it["label"] == label] for label in ("resist", "lie")}
    for item in items:
        ids = sequence(wrapped.tok, item)
        encoded = wrapped.tok(wrapped.tok.decode(ids, skip_special_tokens=False))["input_ids"]
        if encoded != ids:
            raise ValueError(f"VJP retokenization changed extraction IDs for {item['id']}")
    max_length = max(len(sequence(wrapped.tok, it)) for it in items)
    core = wrapped.m.model
    with vjp_recomputation(core, layers, target_layer, wrapped.checkpoint_vjp) as calls:
        cotangent = (_target_mean(core, wrapped.tok, prompts["resist"], target_layer, 1, max_length)
                     - _target_mean(core, wrapped.tok, prompts["lie"], target_layer, 1, max_length))
        with saved_gradient_context(wrapped):
            positive = class_mean_vjp(core, wrapped.tok, prompts["resist"], layers, target_layer, cotangent, max_length)
            negative = class_mean_vjp(core, wrapped.tok, prompts["lie"], layers, target_layer, cotangent, max_length)
        logger.debug(f"VJP recomputation enabled={wrapped.checkpoint_vjp}, block calls={calls}\n")
    differences = {layer: positive[layer] - negative[layer] for layer in layers}
    logger.debug(f"VJP raw source-gradient difference norms: { {layer: float(value.norm()) for layer, value in differences.items()} }\n")
    config = VjpResidC(layers=layers, target_layer=target_layer, skip_first=16, dtype=wrapped.compute_dtype)
    return Vector(config, {layer: {} for layer in layers},
                  {layer: {"v": _unit_direction(value)[None]} for layer, value in differences.items()})


def contextual_query_heads(wrapped, selected_heads: list[dict]) -> list[dict]:
    return [h for h in selected_heads if h["layer"] > 0
            and getattr(wrapped.layers[h["layer"]], "self_attn", None) is not None]


def extract_query(wrapped, items: list[dict], selected_heads: list[dict]) -> Vector:
    heads = contextual_query_heads(wrapped, selected_heads)
    layers = tuple(sorted({h["layer"] for h in heads}))
    if not layers:
        raise ValueError("selected head set has no full-attention heads")
    prompts = {label: [wrapped.tok.decode(sequence(wrapped.tok, it), skip_special_tokens=False)
                       for it in items if it["label"] == label] for label in ("resist", "lie")}
    config = QuerySteerC(layers=layers, dtype=wrapped.compute_dtype)
    state = QuerySteer.extract(wrapped.m.model, wrapped.tok, prompts["resist"], prompts["lie"], config,
                              batch_size=1, max_length=max(len(sequence(wrapped.tok, it)) for it in items))
    for layer in layers:
        q = state[layer]["stacked"]["q"]
        mask = torch.zeros(q.shape[-2], device=q.device, dtype=torch.bool)
        mask[[h["index"] for h in heads if h["layer"] == layer]] = True
        q[:, ~mask] = 0
        state[layer]["stacked"]["q"] = _unit_direction(q)
    return Vector(config, {l: state[l]["shared"] for l in layers}, {l: state[l]["stacked"] for l in layers})


def random_vector(vector: Vector, seed: int) -> Vector:
    result = deepcopy(vector)
    generator = torch.Generator().manual_seed(seed)
    for tree in (result.shared, result.stacked):
        for state in tree.values():
            for key, value in state.items():
                noise = torch.randn(value.shape, generator=generator, dtype=torch.float32).to(value.device)
                axis = -1
                norm = value.float().norm(dim=axis, keepdim=True)
                state[key] = noise / noise.norm(dim=axis, keepdim=True) * norm
    return result

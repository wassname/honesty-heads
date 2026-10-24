"""Frozen native-dtype storage with uncached float32 parameter access. PI/OpenAI."""

from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path

import torch
from torch import Tensor, nn
from torch.nn.utils import parametrize
from safetensors import safe_open
from steering_lite.variants.vjp_resid import _activations

from .data import sequence
from .placement import stream_text_model


class Float32Cast(nn.Module):
    def forward(self, source: Tensor) -> Tensor:
        return source.float()


def enable_on_demand_float32(model) -> dict:
    originals = list(model.named_parameters())
    if any(parameter.requires_grad for _, parameter in originals):
        raise ValueError("on-demand float32 requires frozen model parameters")
    counts = Counter(str(parameter.dtype) for _, parameter in originals)
    for module in list(model.modules()):
        for name, parameter in list(module.named_parameters(recurse=False)):
            if not parameter.is_floating_point():
                raise TypeError(f"nonfloating parameter {name}: {parameter.dtype}")
            parametrize.register_parametrization(module, name, Float32Cast(), unsafe=True)
            if module.parametrizations[name].original is not parameter:
                raise AssertionError("parametrization replaced the stored original")
    model.config.dtype = torch.float32
    model.model.config.dtype = torch.float32
    return {"compute_dtype": "float32", "stored_parameter_dtypes": dict(counts),
            "global_parameter_cast_cache": False, "frozen": True}


def check_tiny_cast_equivalence(wrapped, item: dict) -> dict:
    device = str(wrapped.m.model.embed_tokens.weight.device)
    placement = {"model.embed_tokens": device, "model.norm": device, "model.rotary_emb": device, "lm_head": device,
                 **{f"model.layers.{layer}": device for layer in range(wrapped.L)}}
    resident = stream_text_model(deepcopy(wrapped.cfg), wrapped.checkpoint_source, torch.float32,
                                 placement, wrapped.m.config._attn_implementation).eval().requires_grad_(False)
    ids = sequence(wrapped.tok, item)
    values, gradients = [], []
    allocations = []
    for model, offload in ((resident, None), (wrapped.m, None), (wrapped.m, True), (wrapped.m, False), (wrapped.m, False)):
        tokens = torch.tensor(ids, device=device)[None]
        context = nullcontext() if offload is None else torch.autograd.graph.save_on_cpu(pin_memory=offload)
        with context, torch.enable_grad(), _activations(model, (0, wrapped.L - 1), graph_root=0) as acts:
            logits = model(input_ids=tokens, use_cache=False, logits_to_keep=1).logits[:, -1].float()
            target = acts[wrapped.L - 1][:, -1].float().square().mean()
            gradient = torch.autograd.grad(target, acts[0])[0]
        values.append(logits.detach().cpu())
        gradients.append(gradient.detach().cpu())
        del logits, target, gradient, acts
        if torch.cuda.is_available() and device.startswith("cuda"):
            torch.cuda.synchronize()
            allocations.append(torch.cuda.memory_allocated())
    for value, gradient in zip(values[1:], gradients[1:], strict=True):
        torch.testing.assert_close(value, values[0], atol=2e-5, rtol=2e-5)
        torch.testing.assert_close(gradient, gradients[0], atol=2e-5, rtol=2e-5)
    index = json.loads((Path(wrapped.checkpoint_source) / "model.safetensors.index.json").read_text())["weight_map"]
    checked = set()
    for module_name, module in wrapped.m.named_modules():
        for name, state in getattr(module, "parametrizations", {}).items():
            original = state.original
            if id(original) in checked:
                continue
            checked.add(id(original))
            key = f"{module_name}.{name}"
            with safe_open(f"{wrapped.checkpoint_source}/{index[key]}", framework="pt", device="cpu") as source:
                expected = source.get_tensor(key)
            if original.dtype != expected.dtype or not torch.equal(original.detach().cpu(), expected):
                raise AssertionError(f"stored checkpoint tensor changed: {key}")
            if getattr(module, name).dtype != torch.float32:
                raise AssertionError(f"parameter access is not float32: {key}")
    embedding = wrapped.m.model.embed_tokens.parametrizations.weight.original
    head = wrapped.m.lm_head.parametrizations.weight.original
    if embedding is not head:
        raise AssertionError("tied embedding/head originals were separated")
    norm = wrapped.m.model.norm.parametrizations.weight.original.detach().cpu()
    if torch.equal(norm, norm.bfloat16().float()):
        raise AssertionError("fixture lacks a genuine non-BF16-representable FP32 sentinel")
    return {"same_stored_checkpoint": True, "same_compute_device": device,
            "modes": ["resident-float32", "cast", "cast-pinned-cpu", "cast-pageable-cpu", "cast-pageable-cpu-repeat"],
            "all_vocabulary_logits_max_error": max(float((value - values[0]).abs().max()) for value in values[1:]),
            "source_gradient_max_error": max(float((gradient - gradients[0]).abs().max()) for gradient in gradients[1:]),
            "stored_tensors_checked": len(checked), "tied_originals_preserved": True,
            "genuine_float32_sentinel": True, "cuda_allocations_after_repeats": allocations,
            "storage": wrapped.precision, "scope": "tiny mixed-storage checkpoint; not real-model memory or throughput"}

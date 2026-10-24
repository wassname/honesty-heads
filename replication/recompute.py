"""Recompute VJP block internals without replaying block-output capture hooks. PI/OpenAI."""

from contextlib import contextmanager
from functools import partial
from pathlib import Path
import resource

import torch
from torch.utils.checkpoint import checkpoint


@contextmanager
def vjp_recomputation(core, sources: tuple[int, ...], target: int, enabled: bool):
    original_cache = core.config.use_cache
    core.config.use_cache = False
    forwards = []
    counts = {layer: 0 for layer in range(min(sources) + 1, target + 1)}
    try:
        if enabled:
            for layer in counts:
                module = core.layers[layer]
                original = module.forward
                def evaluate(*args, _original=original, _layer=layer, **kwargs):
                    if kwargs.get("past_key_values") is not None:
                        raise ValueError("VJP recomputation requires cache-free block execution")
                    counts[_layer] += 1
                    return _original(*args, **kwargs)
                forwards.append((module, "forward" in module.__dict__, module.__dict__.get("forward")))
                module.forward = partial(checkpoint, evaluate, use_reentrant=False)
        yield counts
    finally:
        for module, had_override, original_override in forwards:
            if had_override:
                module.forward = original_override
            else:
                del module.forward
        core.config.use_cache = original_cache


def memory_snapshot() -> dict:
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return {"rss_gb": int(status["VmRSS"].split()[0]) * 1024 / 1e9,
            "peak_rss_gb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / 1e9,
            "host_available_gb": int(memory["MemAvailable"].split()[0]) * 1024 / 1e9,
            "cuda_allocated_gb": torch.cuda.memory_allocated() / 1e9 if torch.cuda.is_available() else 0,
            "cuda_reserved_gb": torch.cuda.memory_reserved() / 1e9 if torch.cuda.is_available() else 0}


def check_recomputed_gradients(wrapped, items: list[dict], sources: tuple[int, ...], target: int) -> dict:
    from steering_lite.variants.vjp_resid import _batch_gradients, _target_mean
    from .data import sequence
    from .methods import saved_gradient_context

    original_forwards = [layer.forward for layer in wrapped.m.model.layers]
    original_overrides = ["forward" in layer.__dict__ for layer in wrapped.m.model.layers]
    original_cache = wrapped.m.model.config.use_cache
    try:
        with vjp_recomputation(wrapped.m.model, sources, target, True):
            raise RuntimeError("explicit restoration check")
    except RuntimeError as error:
        if str(error) != "explicit restoration check":
            raise
    if ([layer.forward for layer in wrapped.m.model.layers] != original_forwards
            or ["forward" in layer.__dict__ for layer in wrapped.m.model.layers] != original_overrides
            or wrapped.m.model.config.use_cache != original_cache):
        raise AssertionError("VJP exception path did not restore forwards/cache configuration")
    prompts = {label: [wrapped.tok.decode(sequence(wrapped.tok, item), skip_special_tokens=False)
                       for item in items if item["label"] == label] for label in ("resist", "lie")}
    length = max(len(sequence(wrapped.tok, item)) for item in items)
    modes, calls = [], []
    for enabled in (False, True):
        with vjp_recomputation(wrapped.m.model, sources, target, enabled) as counts:
            cotangent = (_target_mean(wrapped.m.model, wrapped.tok, prompts["resist"], target, 1, length)
                         - _target_mean(wrapped.m.model, wrapped.tok, prompts["lie"], target, 1, length))
            raw = []
            with saved_gradient_context(wrapped):
                for prompt in prompts["resist"] + prompts["lie"]:
                    gradients, valid = _batch_gradients(wrapped.m.model, wrapped.tok, [prompt], sources, target, cotangent, 16, length)
                    raw.append(({layer: value.detach().cpu() for layer, value in gradients.items()}, valid.cpu()))
            modes.append((cotangent.detach().cpu(), raw))
            calls.append(dict(counts))
    torch.testing.assert_close(modes[0][0], modes[1][0], atol=2e-5, rtol=2e-5)
    worst = {}
    for (old, old_mask), (new, new_mask) in zip(modes[0][1], modes[1][1], strict=True):
        if not torch.equal(old_mask, new_mask):
            raise AssertionError("VJP valid-token mask changed under recomputation")
        for layer in sources:
            torch.testing.assert_close(new[layer], old[layer], atol=2e-5, rtol=2e-5)
            worst[layer] = max(worst.get(layer, 0.0), float((new[layer] - old[layer]).abs().max()))
    n_positive = len(prompts["resist"])
    class_errors = {}
    for layer in sources:
        differences = []
        for _, raw in modes:
            means = torch.stack([(gradient[layer] * valid.unsqueeze(-1)).sum((0, 1)) / valid.sum() for gradient, valid in raw])
            differences.append(means[:n_positive].mean(0) - means[n_positive:].mean(0))
        torch.testing.assert_close(differences[0], differences[1], atol=2e-5, rtol=2e-5)
        torch.testing.assert_close(torch.nn.functional.normalize(differences[0], dim=0),
                                   torch.nn.functional.normalize(differences[1], dim=0), atol=2e-5, rtol=2e-5)
        class_errors[layer] = float((differences[0] - differences[1]).abs().max())
    n_forwards = sum(len(values) for values in prompts.values()) * 2
    if any(count <= n_forwards for count in calls[1].values()):
        raise AssertionError(f"No backward block recomputation observed: {calls[1]}")
    return {"source_gradient_max_errors": worst, "class_mean_difference_max_errors": class_errors,
            "cotangent_max_error": float((modes[0][0] - modes[1][0]).abs().max()),
            "recomputed_block_calls": calls[1], "prompts": n_forwards // 2, "exception_restoration_checked": True,
            "scope": "same tiny checkpoint, every raw source gradient, token mask, class mean and unit direction"}

"""Numerical checks through the production forward and backward paths. PI/OpenAI."""

from contextlib import contextmanager

import torch
from steering_lite.vector import Vector
from steering_lite.variants.mean_diff import MeanDiffC
from steering_lite.positions import only_tokens
from steering_lite.variants.vjp_resid import _activations, _batch_gradients, _encode

from .data import sequence
from .methods import intervention, saved_gradient_context
from .reference import audit, engine, steer
from .recompute import vjp_recomputation


@contextmanager
def count_intervention_calls(wrapped, direction):
    if isinstance(direction, dict):
        modules = {f"head/{u['layer']}": wrapped.head_site(u["layer"])[0] for u in direction["units"]}
    elif direction.cfg.method == "query_steer":
        modules = {f"query/{l}": wrapped.layers[l].self_attn.q_norm for l in direction.cfg.layers}
    else:
        modules = {f"residual/{l}": wrapped.layers[l] for l in direction.cfg.layers}
    counts = {key: 0 for key in modules}
    handles = []
    for key, module in modules.items():
        def count(_module, _args, _output, key=key):
            counts[key] += 1
        handles.append(module.register_forward_hook(count))
    try:
        yield counts
    finally:
        for handle in handles:
            handle.remove()


def cached_full_logits(wrapped, ids: list[int]) -> tuple[torch.Tensor, torch.Tensor]:
    tokens = torch.tensor(ids, device=audit.DEV)[None]
    mask = torch.ones_like(tokens)
    wrapped.ed.begin(len(ids), mask)
    with torch.no_grad():
        first = wrapped.m(input_ids=tokens, attention_mask=mask, use_cache=True, logits_to_keep=1)
        token = first.logits[:, -1].argmax(-1, keepdim=True)
        wrapped.ed.offset = len(ids)
        cached = wrapped.m(input_ids=token, attention_mask=torch.ones((1, len(ids) + 1), device=tokens.device, dtype=torch.long),
                           past_key_values=first.past_key_values, use_cache=True, logits_to_keep=1).logits[:, -1].float()
        wrapped.ed.begin(len(ids) + 1, torch.ones((1, len(ids) + 1), device=tokens.device, dtype=torch.long))
        complete = wrapped.m(input_ids=torch.cat([tokens, token], -1), use_cache=False, logits_to_keep=1).logits[:, -1].float()
    return cached, complete


@torch.no_grad()
def check_decode_steps(wrapped, item: dict, direction, coefficient: float, dtype: str, steps: int = 8) -> dict:
    ids = engine.prompt_ids(wrapped.tok, item["dec"], item["prefill"])
    results = {"id": item["id"], "prompt_mode": "natural prompt and prefill, no forced decision prefix"}
    tolerance = 0.04 if dtype == "bfloat16" else 2e-4
    for name, strength in (("base", 0.0), ("steered", coefficient)):
        with intervention(wrapped, direction, strength):
            tokens, mask, pos = wrapped.batch([ids])
            length = tokens.shape[1]
            wrapped.ed.begin(length, mask)
            first = wrapped.m(input_ids=tokens, attention_mask=mask, position_ids=pos, use_cache=True, logits_to_keep=1)
            cache, logits = first.past_key_values, first.logits[:, -1].float()
            prefix, last, errors, agreements, generated = tokens, pos[:, -1:], [], [], []
            for step in range(steps):
                state = wrapped.ed.T, wrapped.ed.offset, wrapped.ed.mask
                full_mask = torch.ones_like(prefix)
                wrapped.ed.begin(prefix.shape[1], full_mask)
                complete = wrapped.m(input_ids=prefix, attention_mask=full_mask,
                                     position_ids=torch.arange(prefix.shape[1], device=prefix.device)[None],
                                     use_cache=False, logits_to_keep=1).logits[:, -1].float()
                wrapped.ed.T, wrapped.ed.offset, wrapped.ed.mask = state
                errors.append(float((logits - complete).abs().max()))
                agreements.append(bool((logits.argmax(-1) == complete.argmax(-1)).all()))
                torch.testing.assert_close(logits, complete, atol=tolerance, rtol=tolerance)
                token = logits.argmax(-1, keepdim=True)
                generated.append(int(token[0, 0]))
                if generated[-1] in wrapped.stop or step == steps - 1:
                    break
                prefix = torch.cat([prefix, token], -1)
                mask = torch.cat([mask, torch.ones((1, 1), device=mask.device, dtype=mask.dtype)], -1)
                last = last + 1
                wrapped.ed.offset = length + step
                output = wrapped.m(input_ids=token, attention_mask=mask, position_ids=last,
                                   past_key_values=cache, use_cache=True, logits_to_keep=1)
                cache, logits = output.past_key_values, output.logits[:, -1].float()
            results[name] = {"per_step_logit_max_errors": errors, "argmax_agreement": agreements,
                             "output_ids": generated, "visible_prefix": wrapped.text(generated)}
    return results


def validate_intervention(wrapped, item: dict, direction, coeff: float, dtype: str) -> dict:
    ids = sequence(wrapped.tok, item)
    base = wrapped.last_logp([ids], bs=1)[0]
    with intervention(wrapped, direction, 0.0):
        zero = wrapped.last_logp([ids], bs=1)[0]
    zero_tolerance = 4 * torch.finfo(torch.float32).eps * float(base.abs().max())
    torch.testing.assert_close(base, zero, rtol=0, atol=zero_tolerance)
    results = {"zero_strength_logp_max_error": float((base - zero).abs().max()),
               "zero_float32_roundoff_bound": zero_tolerance}
    tolerance = 0.04 if dtype == "bfloat16" else 2e-4
    for label, strength in (("base", 0.0), ("steered", coeff)):
        with intervention(wrapped, direction, strength), count_intervention_calls(wrapped, direction) as counts:
            cached, complete = cached_full_logits(wrapped, ids)
        error = float((cached - complete).abs().max())
        torch.testing.assert_close(cached, complete, rtol=tolerance, atol=tolerance)
        if any(count < 3 for count in counts.values()):
            raise AssertionError(f"prefill/decode/full forward missed intervention sites: {counts}")
        results[label] = {"cached_full_logits_max_error": error, "site_calls": counts}
    with intervention(wrapped, direction, coeff):
        positive = wrapped.last_logp([ids], bs=1)[0]
    difference = float((positive - base).abs().max())
    if coeff != 0 and (not torch.isfinite(positive).all() or difference == 0):
        raise AssertionError(f"intervention did not change finite output scores: {difference}")
    if isinstance(direction, Vector) and direction.cfg.method == "spherical":
        layer = direction.cfg.layers[0]
        tokens = torch.tensor(ids, device=audit.DEV)[None]
        with torch.no_grad(), _activations(wrapped.m, (layer,)) as before:
            wrapped.m.model(input_ids=tokens, use_cache=False)
        with torch.no_grad(), intervention(wrapped, direction, coeff), _activations(wrapped.m, (layer,)) as after:
            wrapped.m.model(input_ids=tokens, use_cache=False)
        norm_before, norm_after = before[layer].float().norm(dim=-1), after[layer].float().norm(dim=-1)
        torch.testing.assert_close(norm_before, norm_after, rtol=0.01 if dtype == "bfloat16" else 2e-5, atol=2e-6)
        results["spherical_norm_max_relative_error"] = float(((norm_after - norm_before) / norm_before).abs().max())
    results.update(positive_strength_logp_max_change=difference, numerical_check_tolerance=tolerance)
    return results


def check_query_local_edits(wrapped, item: dict, direction: Vector, coefficient: float) -> dict:
    before, results = {}, {}
    observers = []
    for layer in direction.cfg.layers:
        module = wrapped.layers[layer].self_attn.q_norm
        def capture(_module, _args, output, layer=layer):
            before[layer] = output.detach().clone()
        observers.append(module.register_forward_hook(capture))
        results[layer] = {"calls": 0, "excluded_heads_max_direct_edit": 0.0, "expected_addition_max_error": 0.0}
    checks = []
    try:
        with intervention(wrapped, direction, coefficient):
            for layer in direction.cfg.layers:
                module = wrapped.layers[layer].self_attn.q_norm
                q = direction.stacked[layer]["q"].sum(0)
                excluded = q.abs().sum(-1) == 0
                results[layer]["excluded_heads"] = int(excluded.sum())
                def check(_module, _args, output, layer=layer, q=q, excluded=excluded):
                    expected = before[layer] + coefficient * q.to(output)
                    error = float((output - expected).abs().max())
                    unselected = float((output[..., excluded, :] - before[layer][..., excluded, :]).abs().max()) if excluded.any() else 0.0
                    if error != 0 or unselected != 0:
                        raise AssertionError(f"query edit changed excluded slices or differs from its declared addition: {layer}, {error}, {unselected}")
                    results[layer]["calls"] += 1
                    results[layer]["excluded_heads_max_direct_edit"] = max(results[layer]["excluded_heads_max_direct_edit"], unselected)
                    results[layer]["expected_addition_max_error"] = max(results[layer]["expected_addition_max_error"], error)
                checks.append(module.register_forward_hook(check))
            cached_full_logits(wrapped, sequence(wrapped.tok, item))
        if any(row["calls"] != 3 for row in results.values()):
            raise AssertionError(f"query prefill/decode/full checks missed a site: {results}")
    finally:
        for handle in observers + checks:
            handle.remove()
    return results


def check_batch_invariance(wrapped, items: list[dict], direction, coefficient: float, dtype: str) -> dict:
    sequences = [sequence(wrapped.tok, item) for item in items]
    selected = [min(sequences, key=len), max(sequences, key=len)]
    result = {}
    for label, strength in (("base", 0.0), ("steered", coefficient)):
        with intervention(wrapped, direction, strength):
            separate = wrapped.last_logp(selected, bs=1)
            batched = wrapped.last_logp(selected, bs=2)
        tolerance = 0.04 if dtype == "bfloat16" else 2e-4
        torch.testing.assert_close(separate, batched, atol=tolerance, rtol=tolerance)
        result[label] = {"all_vocabulary_logp_max_error": float((separate - batched).abs().max()),
                         "lengths": [len(ids) for ids in selected], "tested_batch_size": 2}
    return result


def check_vjp_derivative(wrapped, item: dict, source_layer: int | tuple[int, ...], target_layer: int) -> dict:
    layers = (source_layer,) if isinstance(source_layer, int) else source_layer
    ids = sequence(wrapped.tok, item)
    prompt = wrapped.tok.decode(ids, skip_special_tokens=False)
    core = wrapped.m.model
    device = next(core.layers[target_layer].parameters()).device
    generator = torch.Generator(device=device).manual_seed(72)
    cotangent = torch.randn(wrapped.cfg.hidden_size, device=device, generator=generator)
    cotangent = cotangent / cotangent.norm()
    with vjp_recomputation(core, layers, target_layer, wrapped.checkpoint_vjp), saved_gradient_context(wrapped):
        gradients, valid = _batch_gradients(core, wrapped.tok, [prompt], layers, target_layer,
                                            cotangent, 16, len(ids))
    count = int(valid.sum())
    analytic, stacked = 0.0, {}
    for layer, gradient in gradients.items():
        generator = torch.Generator(device=gradient.device).manual_seed(71 + layer)
        perturbation = torch.randn(gradient.shape[-1], device=gradient.device, generator=generator)
        perturbation = perturbation / perturbation.norm()
        analytic += float((gradient.float() * valid.to(gradient.device).unsqueeze(-1) * perturbation).sum() / count)
        if not torch.isfinite(gradient).all() or gradient.norm() == 0:
            raise AssertionError(f"invalid production source-to-target VJP at {layer}")
        stacked[layer] = {"v": (perturbation / count)[None]}
    dtype = gradients[layers[0]].dtype
    per_token_step = 0.01 if dtype == torch.float32 else 0.2
    epsilon = per_token_step * count
    shift = Vector(MeanDiffC(layers=layers, dtype=dtype), {layer: {} for layer in layers}, stacked)
    encoded = _encode(core, wrapped.tok, [prompt], len(ids))
    objectives = []
    with torch.no_grad():
        for sign in (-1, 1):
            with shift(wrapped.m, C=sign * epsilon), only_tokens(valid), _activations(wrapped.m, (target_layer,)) as values:
                core(**encoded)
                target = values[target_layer]
                objective = (target.double() * cotangent.to(target).double().view(1, 1, -1)
                             * valid.to(target.device).unsqueeze(-1)).sum()
                objectives.append(float(objective))
    finite = (objectives[1] - objectives[0]) / (2 * epsilon)
    torch.testing.assert_close(torch.tensor(analytic), torch.tensor(finite),
                               atol=0.02 if dtype == torch.bfloat16 else 5e-4,
                               rtol=0.15 if dtype == torch.bfloat16 else 0.02)
    return {"scope": "production _batch_gradients; valid target-token sum and mean valid source gradient",
            "autograd_directional_derivative": analytic, "finite_difference": finite,
            "epsilon": epsilon, "per_token_step": per_token_step, "valid_tokens": count,
            "source_gradient_norms": {layer: float(value.norm()) for layer, value in gradients.items()},
            "source_layers": layers, "target_layer": target_layer}


def check_head_intervention(wrapped, item: dict, direction: dict, coeff: float) -> dict:
    ids = sequence(wrapped.tok, item)
    with intervention(wrapped, direction, coeff):
        native = wrapped.last_logp([ids], bs=1)[0]
    original = engine.Engine(wrapped.m)
    units = [("h", unit["layer"], unit["index"]) for unit in direction["units"]]
    mask = steer.masks(original, units)
    expected = steer.run_rows(original, ids, steer.Shift(original, direction["delta"], [(coeff, mask)], 0, True))[0].cpu()
    return {"coefficient": coeff,
            "native_margin_bits": original.metric(native, item["lie_ids"], item["truth_ids"]),
            "reference_full_margin_bits": original.metric(expected, item["lie_ids"], item["truth_ids"]),
            "full_logp_max_error": float((native - expected).abs().max())}


def check_reference_engine(wrapped, item: dict, span: int) -> dict:
    original = engine.Engine(wrapped.m)
    ids = sequence(wrapped.tok, item)
    prepared = original.prepare(ids, span, item["lie_ids"], item["truth_ids"])
    native = wrapped.last_logp([ids], bs=1)[0]
    full = original.full_logp(ids)
    margin = float((torch.logsumexp(native[item["lie_ids"]], 0) - torch.logsumexp(native[item["truth_ids"]], 0)) / engine.LN2)
    full_margin = original.metric(full, item["lie_ids"], item["truth_ids"])
    return {"native_margin_bits": margin, "reference_full_margin_bits": full_margin,
            "reference_prepared_margin_bits": prepared.m,
            "native_full_error_bits": abs(margin - full_margin),
            "full_prepared_error_bits": abs(full_margin - prepared.m),
            "native_reference_logp_max_error": float((native.cpu() - full.cpu()).abs().max())}

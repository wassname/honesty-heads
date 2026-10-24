"""Model loading and the original independent native-hook engine. PI/OpenAI."""

from dataclasses import dataclass
from pathlib import Path

import torch
from huggingface_hub import snapshot_download
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

from .placement import stream_text_model
from .precision import enable_on_demand_float32
from .reference import audit


@dataclass
class Config:
    model: str = "Qwen/Qwen3.5-9B"
    revision: str = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
    dtype: str = "bfloat16"
    device: str = "cuda"
    attention_backend: str = "sdpa"
    cpu_tail_layers: int = 0
    on_demand_float32: bool = False
    checkpoint_vjp: bool = False
    seed: int = 41
    tiny: bool = False
    n_select_per_class: int = 400
    n_test_per_class: int = 400
    n_persona_per_half: int = 8
    n_traits: int = 135
    random_directions: int = 39
    max_new_tokens: int = 128
    n_generation_per_stratum: int = 1
    n_persona_generation_per_half: int = 1
    n_controls_per_polarity: int = 1
    method: str = "heads"
    coeff: float = 2.0
    layer: int = 24
    source_layer: int = 16
    target_layer: int | None = None
    span: int = 9
    batch_size: int = 1
    random_start: int = 1
    shuffled_seed: int | None = 41
    calibration_only: bool = False
    calibration_path: str | None = None
    coefficient_grid: tuple[float, ...] = (-4.0, -2.0, -1.0, 0.0, 1.0, 2.0, 4.0)
    reference_coefficient: float = 2.0
    calibration_tolerance_bits: float = 0.002
    calibration_refinements: int = 12
    out: str = "outputs/reference"


def vjp_sites(cfg: Config, n_layers: int) -> tuple[int, int]:
    source = min(cfg.source_layer, n_layers - 2)
    target = max(source + 1, n_layers - 3) if cfg.target_layer is None else cfg.target_layer
    if not 0 <= source < target < n_layers:
        raise ValueError(f"VJP requires 0 <= source < target < {n_layers}, got {source}, {target}")
    return source, target


def load_model(cfg: Config):
    if cfg.calibration_path and (cfg.calibration_only or cfg.method == "all"):
        raise ValueError("A frozen artifact evaluates one method; do not combine it with fitting or method=all")
    if cfg.calibration_tolerance_bits <= 0 or cfg.calibration_refinements < 0:
        raise ValueError("Calibration requires positive numerical tolerance and nonnegative refinement count")
    if cfg.on_demand_float32 and (cfg.dtype != "float32" or cfg.cpu_tail_layers):
        raise ValueError("On-demand float32 is a separate all-device float32-computation path")
    torch.manual_seed(cfg.seed)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    tok = AutoTokenizer.from_pretrained(cfg.model, revision=cfg.revision)
    original = AutoConfig.from_pretrained(cfg.model, revision=cfg.revision)
    text = original.get_text_config()
    checkpoint, revision = cfg.model, cfg.revision
    if cfg.tiny:
        values = text.to_dict()
        values.update(hidden_size=64, intermediate_size=128, num_hidden_layers=4,
                      num_attention_heads=4, num_key_value_heads=2, head_dim=16,
                      max_position_embeddings=4096, use_cache=True)
        if text.model_type == "qwen3_5_text":
            values.update(layer_types=["linear_attention"] * 3 + ["full_attention"],
                          num_attention_heads=2, linear_num_key_heads=2, linear_num_value_heads=4,
                          linear_key_head_dim=8, linear_value_head_dim=8,
                          rope_parameters={"rope_type": "default", "rope_theta": 10000,
                                           "partial_rotary_factor": 0.25,
                                           "mrope_section": [1, 1, 0], "mrope_interleaved": True})
        else:
            values.update(layer_types=["full_attention"] * 4,
                          rope_parameters={"rope_type": "default", "rope_theta": 10000})
        if cfg.on_demand_float32:
            values["tie_word_embeddings"] = True
        text = type(text)(**values)
        random_model = AutoModelForCausalLM.from_config(text, attn_implementation=cfg.attention_backend)
        random_model = random_model.to(dtype=torch.bfloat16 if cfg.on_demand_float32 else getattr(torch, cfg.dtype))
        if cfg.on_demand_float32:
            random_model.model.norm.float()
            with torch.no_grad():
                random_model.model.norm.weight.copy_(1 + torch.arange(text.hidden_size, dtype=torch.float32) / 10000)
        checkpoint = str(Path(cfg.out) / "random_checkpoint")
        state = random_model.state_dict()
        if cfg.cpu_tail_layers and text.model_type == "qwen3_5_text":
            state = {"model.language_model." + name.removeprefix("model.") if name.startswith("model.") else name: value
                     for name, value in state.items()}
        random_model.save_pretrained(checkpoint, state_dict=state, safe_serialization=True, max_shard_size="1MB")
        del random_model, state
        revision = None
    if cfg.cpu_tail_layers or cfg.on_demand_float32:
        device_map = split_device_map(text.num_hidden_layers, cfg.cpu_tail_layers, cfg.device) if cfg.cpu_tail_layers else {
            "model.embed_tokens": cfg.device, "model.norm": cfg.device, "model.rotary_emb": cfg.device,
            "lm_head": cfg.device, **{f"model.layers.{layer}": cfg.device for layer in range(text.num_hidden_layers)}}
        snapshot = checkpoint if cfg.tiny else snapshot_download(
            cfg.model, revision=cfg.revision, allow_patterns=["config.json", "*.safetensors", "*.index.json"])
        model = stream_text_model(text, snapshot, getattr(torch, cfg.dtype), device_map, cfg.attention_backend,
                                  preserve_storage_dtype=cfg.on_demand_float32)
    else:
        model = AutoModelForCausalLM.from_pretrained(
            checkpoint, revision=revision, dtype=getattr(torch, cfg.dtype),
            attn_implementation=cfg.attention_backend, device_map=cfg.device,
        )
    model.eval().requires_grad_(False)
    precision = enable_on_demand_float32(model) if cfg.on_demand_float32 else {"compute_dtype": cfg.dtype}
    audit.DEV = cfg.device
    wrapped = audit.Model(cfg.model, cfg.dtype, model=model, tok=tok)
    install_device_safe_head_editor(wrapped)
    wrapped.compute_dtype = getattr(torch, cfg.dtype)
    wrapped.on_demand_float32 = cfg.on_demand_float32
    wrapped.checkpoint_vjp = cfg.checkpoint_vjp
    wrapped.precision = precision
    wrapped.checkpoint_source = checkpoint
    return wrapped


def split_device_map(n_layers: int, cpu_tail_layers: int, device: str) -> dict[str, str]:
    if device != "cuda" or not 0 < cpu_tail_layers < n_layers:
        raise ValueError("CPU tail placement requires cuda and a proper nonempty suffix of layers")
    return {"model.embed_tokens": "cpu", "model.norm": "cpu", "model.rotary_emb": "cpu", "lm_head": "cpu",
            **{f"model.layers.{layer}": "cpu" if layer >= n_layers - cpu_tail_layers else "cuda:0"
               for layer in range(n_layers)}}


def install_device_safe_head_editor(wrapped) -> None:
    editor = wrapped.ed
    for handle in editor.hooks:
        handle.remove()
    editor.hooks = []

    def hook(module, args):
        if module not in editor.plan:
            return editor._hook(module, args)
        original_mask, original_ops = editor.mask, editor.plan[module]
        device = args[0].device
        editor.mask = original_mask.to(device)
        editor.plan[module] = [(mod, kind, cols.to(device), where, values.to(device))
                               for mod, kind, cols, where, values in original_ops]
        try:
            return editor._hook(module, args)
        finally:
            editor.mask, editor.plan[module] = original_mask, original_ops

    for layer in range(wrapped.L):
        for kind in ("head", "neuron"):
            editor.hooks.append(wrapped.site(kind, layer).register_forward_pre_hook(hook))

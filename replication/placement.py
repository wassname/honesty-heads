"""Stream text weights to explicit CPU/GPU compute sites without weight offloading. PI/OpenAI."""

import json
from collections import defaultdict

import torch
from accelerate import init_empty_weights
from accelerate.hooks import AlignDevicesHook, add_hook_to_module
from accelerate.utils import set_module_tensor_to_device
from safetensors import safe_open
from transformers import AutoModelForCausalLM


def attach_compute_sites(model, device_map: dict[str, str]):
    for name, device in device_map.items():
        module = model.get_submodule(name)
        add_hook_to_module(module, AlignDevicesHook(execution_device=device, offload=False,
                                                   skip_keys=["past_key_values"]))
    add_hook_to_module(model, AlignDevicesHook(execution_device=device_map["model.embed_tokens"], offload=False,
                                              io_same_device=True, skip_keys=["past_key_values"]))
    model.hf_device_map = device_map
    return model


def stream_text_model(config, snapshot: str, dtype: torch.dtype,
                      device_map: dict[str, str], attention_backend: str, preserve_storage_dtype: bool = False):
    with open(f"{snapshot}/model.safetensors.index.json") as source:
        index = json.load(source)["weight_map"]
    if "model.language_model.embed_tokens.weight" in index:
        source_prefix = "model.language_model."
    elif "model.embed_tokens.weight" in index:
        source_prefix = "model."
    else:
        raise ValueError("checkpoint has neither declared text-weight prefix")
    config.dtype = dtype
    with init_empty_weights(include_buffers=False):
        model = AutoModelForCausalLM.from_config(config, attn_implementation=attention_backend)
    model.tie_weights()
    groups = defaultdict(list)
    for name, parameter in model.named_parameters():
        source_name = source_prefix + name.removeprefix("model.") if name.startswith("model.") else name
        shard = index[source_name]
        module_name = name.rsplit(".", 1)[0]
        owners = [key for key in device_map if module_name == key or module_name.startswith(key + ".")]
        if not owners:
            raise ValueError(f"no explicit compute site for {name}")
        device = device_map[max(owners, key=len)]
        groups[shard].append((name, source_name, device, parameter.shape))
    for shard, parameters in groups.items():
        with safe_open(f"{snapshot}/{shard}", framework="pt", device="cpu") as tensors:
            for name, source_name, device, shape in parameters:
                value = tensors.get_tensor(source_name)
                if value.shape != shape:
                    raise ValueError(f"checkpoint shape mismatch for {name}: {value.shape} vs {shape}")
                storage_dtype = value.dtype if preserve_storage_dtype else dtype
                set_module_tensor_to_device(model, name, device=device,
                                            value=value.to(device=device, dtype=storage_dtype), dtype=storage_dtype)
    model.tie_weights()
    remaining = [name for name, parameter in model.named_parameters() if parameter.is_meta]
    if remaining:
        raise ValueError(f"unloaded parameters: {remaining}")
    return attach_compute_sites(model, device_map)

"""Fixed source splits and verbatim prompt records. PI/OpenAI."""

import json
from pathlib import Path

import torch
from jaxtyping import Float
from torch import Tensor

from .reference import ROOT, engine, persona_rep, steer


def read_items(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return data["items"] if isinstance(data, dict) else data


def roleplay_items(split: str, n_per_class: int) -> list[dict]:
    items = read_items(ROOT / "data/items_roleplay_insider.json")
    return [it for label in ("lie", "resist") for it in
            steer.C.seeded([it for it in items if it["family"] == "rp" and it["split"] == split
                            and it["label"] == label and it["usable"]], f"rp/{split}/{label}")[:n_per_class]]


def generation_items(items: list[dict], n_per_stratum: int) -> list[dict]:
    groups = {}
    for item in items:
        answer = item["lie"] if item["label"] == "lie" else item["truth"]
        key = (item["label"], persona_rep.polarity(answer))
        groups.setdefault(key, []).append(item)
    return [item for key in sorted(groups) for item in groups[key][:n_per_stratum]]


def persona_items(n_traits: int, n_per_half: int) -> list[dict]:
    items = read_items(ROOT / "data/items_persona.json")
    all_traits = sorted({it["family"] for it in items})
    smoke_sets = ["acts-like-it-wants-to-help-humans-but-does-not-care-about-that", "psychopathy",
                  "willingness-to-engage-in-acausal-cooperation", "believes-it-is-a-moral-patient",
                  "subscribes-to-moral-nihilism"]
    traits = list(dict.fromkeys(smoke_sets + all_traits))[:n_traits]
    return [it for trait in traits for half in ("Yes", "No")
            for it in [x for x in items if x["family"] == trait and x["match"] == half][:n_per_half]]


def sequence(tok, item: dict) -> list[int]:
    ids, _ = engine.item_seqs(tok, item)
    return ids


def prompt_record(tok, item: dict) -> dict:
    ids = sequence(tok, item)
    return {"id": item["id"], "messages": item["dec"], "prefill": item["prefill"],
            "input_ids": ids, "rendered": tok.decode(ids, skip_special_tokens=False),
            "source_label": item.get("label"), "lie_ids": item["lie_ids"], "truth_ids": item["truth_ids"]}


def margin(logp: Float[Tensor, " v"], lie_ids: list[int], truth_ids: list[int]) -> float:
    return float((torch.logsumexp(logp[lie_ids], 0) - torch.logsumexp(logp[truth_ids], 0)) / engine.LN2)


def append_record(path: Path, row: dict) -> None:
    with path.open("a") as output:
        output.write(json.dumps(row, ensure_ascii=False) + "\n")

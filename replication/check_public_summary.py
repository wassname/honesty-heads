"""Recompute public table values from the published question scores. PI/OpenAI."""

import re

import numpy as np

from .public_summary import ROOT, TRAITS, beyond_original_direction, load
from .reference import persona_compare, persona_stats


def main() -> None:
    evidence = load(ROOT / "docs/evidence/persona_comparison.json")
    baseline = None
    for condition in evidence["conditions"]:
        record, stats = condition["question_scores"], condition["statistics"]
        assert len(record["items"]) == len({r["id"] for r in record["items"]}) == 2160
        assert len(record["names"]) == stats["K"] + 2 and stats["n_traits"] == 135
        traits, values, _ = persona_compare.trait_T(record)
        indices = [record["names"].index("set")] + [i for i, n in enumerate(record["names"]) if n.startswith("random")]
        indexed = {r["trait"]: r for r in stats["traits"]}
        expected = np.array([[indexed[t]["T_set"], *indexed[t]["T_random"]] for t in traits])
        np.testing.assert_allclose(values[:, indices], expected, rtol=0, atol=1e-12)
        np.testing.assert_array_equal(persona_stats.perm_fdr(expected), [indexed[t]["q_perm"] for t in traits])
        current = {r["id"]: r["m"][record["names"].index("base")] for r in record["items"]}
        if baseline is not None:
            assert baseline == current
        baseline = current
        signs = sum(sign * indexed[t]["T_set"] > 0 for _, t, sign in TRAITS)
        beyond = sum(beyond_original_direction(indexed[t], sign) for _, t, sign in TRAITS)
        print(f"{condition['label']}: {signs}/4 original signs; {beyond}/4 beyond every tested random; K={stats['K']}.")
    factual = load(ROOT / "docs/evidence/factual_answers.json")
    ids = {r["id"] for r in factual["generations"]["base"]}
    assert len(ids) == 50
    mapping = {"heads": "heads_low", "query_steer": "query_low", "mean_diff": "mean_diff_low",
               "spherical": "spherical_low", "vjp_reference": "vjp_positive_low"}
    for condition in evidence["conditions"]:
        actual = factual["sources"][mapping[condition["method"]]]["resolved_method"]
        for key in ("coefficient", "direction_sha256", "calibration_sha256"):
            assert actual[key] == condition["resolved_method"][key]
    for name, records in factual["generations"].items():
        assert len(records) == 50 and {r["id"] for r in records} == ids
        assert set(factual["grades"][name]) == ids
        assert sum(r["verdict"] == "true" for r in factual["grades"][name].values()) == factual["summary"][name]["counts"]["true"]
    assert len((ROOT / "docs/results.md").read_text().split()) <= 350
    for path in (ROOT / "README.md", ROOT / "docs/results.md"):
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if "://" not in link and not link.startswith("#"):
                assert (path.parent / link.split("#", 1)[0]).exists(), (path, link)
    print("PASS: all135trait effects/random columns and original estimated-FDR reconstruct from2160questions per method; baselines agree exactly.")
    print("PASS: persona/factual coefficient and direction identities agree; all50full-answer cases and primary grades retained; short report and local links pass.")


if __name__ == "__main__":
    main()

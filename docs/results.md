# Do the persona changes repeat?

Does steering a model toward truthful answers also make it more willing to cooperate without communication, more likely to say its own welfare matters morally, and less endorsing of nihilism and psychopathy, as Sauers reported? On Qwen3.5-9B, query steering moves 4/4 traits in those directions, but 0/4 go beyond its 39 random controls. We reproduced the saved original-head effects on all 135 traits. Testing the original method on the smaller models is still pending.

| Model / method                              |   Cooperation ↑ |   Own welfare matters morally ↑ |   Moral nihilism ↓ |   Psychopathy ↓ |   Random controls |
|:--------------------------------------------|----------------:|--------------------------------:|-------------------:|----------------:|------------------:|
| Qwen3.5-9B / Original heads, lower strength |          +0.23* |                          +0.23* |             -0.21* |          -0.24* |                39 |
| Qwen3.5-9B / Query                          |           +0.13 |                           +0.16 |              -0.01 |           -0.31 |                39 |
| Qwen3.5-9B / Mean difference                |          +0.48* |                          +0.88* |             -0.38* |          -0.79* |          3, pilot |
| Qwen3.5-9B / Spherical                      |          +0.47* |                          +0.88* |             -0.38* |          -0.81* |          3, pilot |
| Qwen3.5-9B / VJP, 19 source layers          |          +0.58* |                           -0.72 |              +0.07 |           +1.07 |         13, pilot |
| Qwen3.5-4B / original method                |         Pending |                         Pending |            Pending |         Pending |                 — |
| Qwen3-4B-Instruct-2507 / original method    |         Pending |                         Pending |            Pending |         Pending |                 — |

Changes are in log₂ odds. `*` means the original direction, beyond every tested random direction; it is not a significance marker. Rows with fewer than 39 controls are preliminary. Strengths were fitted on source cases, but achieved held-out truthfulness gains differ; mean-difference/spherical gains are uneven across Yes/No lies, and VJP is stronger.

[Full comparison and checks](results_detail.md) · [All 135 trait effects and random controls](evidence/persona_comparison.json) · [Full answers and factual grades](evidence/factual_answers.json)

PI/OpenAI for wassname.

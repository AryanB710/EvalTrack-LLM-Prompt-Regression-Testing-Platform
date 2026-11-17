# USD per 1M tokens as (input, output). Prices change often; check the
# provider's pricing page and edit here. Unknown models are costed at 0.
PRICES = {
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "gpt-4o-mini": (0.15, 0.60),
    "mock-small": (0.05, 0.08),
    "mock-large": (0.59, 0.79),
}


def cost_usd(model, prompt_tokens, completion_tokens):
    inp, out = PRICES.get(model, (0.0, 0.0))
    return (prompt_tokens * inp + completion_tokens * out) / 1_000_000

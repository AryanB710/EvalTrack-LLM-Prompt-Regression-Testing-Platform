import json


def pct(value, digits=1):
    return "–" if value is None else f"{value * 100:.{digits}f}%"


def ms(value):
    return "–" if value is None else f"{value:,.0f} ms"


def usd(value):
    if value is None:
        return "–"
    return f"${value:.6f}" if value < 0.01 else f"${value:.4f}"


def delta(value, unit="%", lower_is_better=True):
    """Render a +/- change with a css class saying whether it's good or bad."""
    if value is None:
        return {"text": "–", "cls": ""}
    if abs(value) < 0.05:
        return {"text": f"±0{unit}", "cls": "neutral"}
    good = value < 0 if lower_is_better else value > 0
    sign = "+" if value > 0 else ""
    return {"text": f"{sign}{value:.1f}{unit}", "cls": "good" if good else "bad"}


def register(app):
    app.jinja_env.filters["pct"] = pct
    app.jinja_env.filters["ms"] = ms
    app.jinja_env.filters["usd"] = usd
    app.jinja_env.filters["fromjson"] = json.loads
    app.jinja_env.globals["delta"] = delta

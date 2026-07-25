"""Gemeinsame Zahlen-Formatierung fuer Flo.

Ueberall, wo dem Nutzer eine (grosse) Zahl angezeigt wird, sollen Tausender mit
einem PUNKT getrennt werden (deutsche Schreibweise): 1000000 -> 1.000.000,
-2500 -> -2.500. So sehen alle Discord-Nachrichten (und das Panel) einheitlich
aus.

    from numfmt import fmt
    f"Du hast {fmt(coins)} Flo Coins."
"""


def fmt(n):
    """Ganzzahl mit deutschen Tausenderpunkten. Robust: Murks -> str(n).

    Faengt auch inf/NaN ab: int(float('inf')) wirft einen OverflowError, und der
    ist frueher bis in die Nachricht durchgeschlagen."""
    if isinstance(n, int) and not isinstance(n, bool):
        return f"{n:,}".replace(",", ".")     # exakt, auch bei riesigen Zahlen
    try:
        wert = float(n)
    except (TypeError, ValueError):
        return str(n)
    if wert != wert or wert in (float("inf"), float("-inf")):
        return str(n)
    try:
        return f"{int(round(wert)):,}".replace(",", ".")
    except (TypeError, ValueError, OverflowError):
        return str(n)

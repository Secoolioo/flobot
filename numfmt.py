"""Gemeinsame Zahlen-Formatierung fuer Flo.

Ueberall, wo dem Nutzer eine (grosse) Zahl angezeigt wird, sollen Tausender mit
einem PUNKT getrennt werden (deutsche Schreibweise): 1000000 -> 1.000.000,
-2500 -> -2.500. So sehen alle Discord-Nachrichten (und das Panel) einheitlich
aus.

    from numfmt import fmt
    f"Du hast {fmt(coins)} Flo Coins."
"""


def fmt(n):
    """Ganzzahl mit deutschen Tausenderpunkten. Robust: Murks -> str(n)."""
    try:
        return f"{int(round(float(n))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(n)

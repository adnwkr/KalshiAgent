"""Helpers shared by the NFL, NBA and PGA models.

Each model adds the repo's common/ folder to sys.path and imports from here, so a fix to the
fee math or odds conversion lands in every sport at once.
"""
import math
import re
import unicodedata

DEFAULT_ORDER_DOLLARS = 10.0  # matches the $10-a-bet bankroll on the Notion hub


def price_to_american(c):
    """Kalshi cost in cents (fee included) -> American odds for the Bet Log and grader."""
    c = min(max(c, 0.5), 99.5)
    return round(-100 * c / (100 - c)) if c >= 50 else round(100 * (100 - c) / c)


def fee(c, coef=0.07, order_dollars=DEFAULT_ORDER_DOLLARS):
    """Kalshi taker fee in cents per contract for an order of about `order_dollars`.

    Kalshi charges ceil(coef * n * P * (1 - P)) dollars, rounded up to the next cent, on the
    whole order of n contracts, not on each contract. Rounding each contract up overstated
    the fee by up to 1c, most on cheap contracts. The order is sized as many whole contracts
    as `order_dollars` buys at price c (at least one).
    """
    if c <= 0 or c >= 100:
        return 0.0
    p = c / 100
    n = max(1, int((order_dollars * 100) // c))
    total_cents = math.ceil(round(coef * n * p * (1 - p) * 100, 6))
    return round(total_cents / n, 2)


_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "đ": "d", "Đ": "D",
                           "ł": "l", "Ł": "L", "þ": "th", "ð": "d", "œ": "oe"})


def norm_name(s):
    """'Ludvig Åberg' and 'Ludvig Aberg' -> 'ludvig aberg'; drops Jr/Sr/II-IV suffixes.
    Letters with no accent-free decomposition (ø, æ, ß...) are spelled out first, so
    'Rasmus Højgaard' matches 'Rasmus Hojgaard'."""
    s = unicodedata.normalize("NFKD", (s or "").translate(_TRANSLIT)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z ]", " ", s.lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def same_player(a, b):
    """True when two spellings name the same golfer: identical after norm_name, or the same last
    name and first initial ('Zach Bauchou' / 'Zachary Bauchou', 'Kris Ventura' / 'Kristoffer Ventura')."""
    x, y = norm_name(a), norm_name(b)
    if x == y:
        return True
    xs, ys = x.split(), y.split()
    return len(xs) >= 2 and len(ys) >= 2 and xs[-1] == ys[-1] and xs[0][0] == ys[0][0]

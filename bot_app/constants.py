from __future__ import annotations

SUPPORTED_CURRENCY_ALIASES = {
    "USD": {
        "usd", "us$", "дол", "долл", "доллар", "доллара", "долларов",
        "доллары", "долларами", "бакс", "бакса", "баксов", "баксы",
        "dollar", "dollars",
    },
    "USDT": {
        "usdt", "usdc", "tether", "тетчер", "тезер", "тезера", "тезеров",
        "юсдт", "юсдц",
    },
    "RUB": {
        "rub", "rur", "руб", "руб.", "рубль", "рубля", "рублей",
        "рубли", "рублями",
    },
    "EUR": {
        "eur", "euro", "евро", "еврик", "еврика", "евриков",
    },
    "TRY": {
        "try", "tl", "try₺", "lira", "liras", "turkishlira",
        "лира", "лиры", "лиру", "лир", "лирами",
    },
    "KZT": {
        "kzt", "тенге", "тг", "tg",
    },
}


def build_alias_map(source: dict[str, set[str]]) -> dict[str, str]:
    return {alias: code for code, aliases in source.items() for alias in aliases}


ALIAS_TO_CURRENCY = build_alias_map(SUPPORTED_CURRENCY_ALIASES)
SUPPORTED_CURRENCY_ORDER = ["USD", "USDT", "RUB", "EUR", "TRY", "KZT"]

OCR_EXTRA_ALIASES = {
    "₽": "RUB",
    "РУБЛЕЙ": "RUB",
    "РУБЛЯ": "RUB",
    "РУБЛИ": "RUB",
    "РУБ": "RUB",
    "$": "USD",
    "USD": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "₺": "TRY",
    "TRY": "TRY",
    "₸": "KZT",
    "KZT": "KZT",
    "USDT": "USDT",
    "USDC": "USDT",
}

OCR_ALIAS_TO_CODE = {
    **OCR_EXTRA_ALIASES,
    **{alias.upper(): code for alias, code in ALIAS_TO_CURRENCY.items()},
}

from django import template

register = template.Library()


@register.filter
def ru_plural(count, forms):
    """Russian plural: {{ n|ru_plural:"часть,части,частей" }} -> "3 части"."""
    one, few, many = [f.strip() for f in forms.split(",")]
    n = abs(int(count))
    if 11 <= n % 100 <= 14:
        word = many
    elif n % 10 == 1:
        word = one
    elif 2 <= n % 10 <= 4:
        word = few
    else:
        word = many
    return f"{count} {word}"

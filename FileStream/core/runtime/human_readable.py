"""Human-readable size formatting utilities."""


def humanbytes(size):
    if size is None:
        return ""
    try:
        size = float(size)
    except Exception:
        return ""
    if size <= 0:
        return "0 B"
    power = 2**10
    n = 0
    Dic_powerN = {0: '', 1: 'Ki', 2: 'Mi', 3: 'Gi', 4: 'Ti'}
    while size >= power and n < 4:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + Dic_powerN[n] + 'B'

"""Five-field cron lines (UTC) -> systemd ``OnCalendar`` expressions.

``bay.toml`` ``[[jobs]] schedule`` is a cron line in UTC. The box runs each
job from a systemd timer, so ``bay compile`` converts the line once, here, and
writes the result next to it (``jobs.<name>.on_calendar`` in services.yml). A
line this module cannot express is a compile error, never a timer that fires
at the wrong time.

Supported in every field: ``*``, a number, a range ``a-b``, a step ``*/n`` or
``a-b/n`` or ``a/n``, and comma lists of these. Month and weekday also take
three-letter English names (``jan``, ``mon``). Weekday 0 and 7 are Sunday.

Not supported: a line that restricts both the day of the month and the day of
the week. Cron runs it when EITHER matches; systemd only when BOTH match.
"""

from __future__ import annotations

_FIELDS = (
    # name, low, high
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _number(text: str, field: int) -> int:
    name, low, high = _FIELDS[field]
    word = text.lower()
    if field == 3 and word in _MONTHS:
        return _MONTHS.index(word) + 1
    if field == 4 and word in _DAYS:
        return _DAYS.index(word)
    if not text.isdigit():
        raise ValueError(f"{name}: {text!r} is not a number")
    value = int(text)
    if not low <= value <= high:
        raise ValueError(f"{name}: {value} is outside {low}-{high}")
    return value


def _values(part: str, field: int) -> list[int]:
    """Every value one comma part of a field selects, sorted."""
    name, low, high = _FIELDS[field]
    base, slash, step_text = part.partition("/")
    step = 1
    if slash:
        if not step_text.isdigit() or int(step_text) < 1:
            raise ValueError(f"{name}: step {step_text!r} must be a whole number of 1 or more")
        step = int(step_text)
    if base == "*":
        start, end = low, high
    elif "-" in base:
        a, _, b = base.partition("-")
        start, end = _number(a, field), _number(b, field)
        if start > end:
            raise ValueError(f"{name}: range {base!r} runs backwards")
    else:
        start = _number(base, field)
        end = high if slash else start
    out = list(range(start, end + 1, step))
    if field == 4:
        out = sorted({v % 7 for v in out})
    return out


def _field(text: str, field: int) -> str:
    """One cron field as an OnCalendar component (``*`` stays ``*``)."""
    _, low, _ = _FIELDS[field]
    if text == "*":
        return "*"
    if text.startswith("*/") and field < 4:
        step = text[2:]
        if not step.isdigit() or int(step) < 1:
            raise ValueError(f"{_FIELDS[field][0]}: step {step!r} must be a whole number of 1 or more")
        return f"{low:02d}/{int(step)}"
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ValueError(f"{_FIELDS[field][0]}: empty item in {text!r}")
        values.update(_values(part, field))
    if field == 4:
        return ",".join(_DAY_NAMES[v] for v in sorted(values))
    return ",".join(f"{v:02d}" for v in sorted(values))


def to_on_calendar(line: str) -> str:
    """``"30 2 * * 1-5"`` -> ``"Mon,Tue,Wed,Thu,Fri *-*-* 02:30:00 UTC"``.

    Raises ValueError with a sentence that names the field.
    """
    parts = line.split()
    if len(parts) != 5:
        raise ValueError(f"{line!r} has {len(parts)} fields; a cron line has five")
    minute, hour, dom, month, dow = (_field(p, i) for i, p in enumerate(parts))
    if dom != "*" and dow != "*":
        raise ValueError(
            "it sets both the day of the month and the day of the week; cron runs the "
            "job when either matches, a timer only when both do. Set one of them to *"
        )
    day = f"{dow} " if dow != "*" else ""
    return f"{day}*-{month}-{dom} {hour}:{minute}:00 UTC"

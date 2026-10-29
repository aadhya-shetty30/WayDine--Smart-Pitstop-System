"""
Parser for the common OpenStreetMap opening_hours formats.

Supported (rule-based, no ML):
    "24/7"
    "08:00-22:00"                       (every day)
    "Mo-Su 08:00-23:00"
    "Mo-Fr 09:00-18:00; Sa 10:00-14:00; Su off"
    "Mo,We,Fr 08:00-12:00"
    "Mo-Sa 11:00-15:00,19:00-23:00"     (several ranges in one day)
    "Mo-Fr 08:00-12:00, Sa 09:00-11:00" (comma between rules)
    "18:00-02:00"                       (closes past midnight)

A schedule is stored as {weekday: [(start_min, end_min), ...]} with weekday
0 = Monday. end_min may go past 1440 when a place closes after midnight,
e.g. 18:00-02:00 -> (1080, 1560).

Anything we cannot parse falls back to the default hours from the config
table and is flagged hours_verified = False.
"""
import re
from collections import Counter

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

# Order matters: longer / more specific patterns first
TOKEN_RE = re.compile(
    r"(?P<range>(?P<h1>\d{1,2}):(?P<m1>\d{2})\s*-\s*(?P<h2>\d{1,2}):(?P<m2>\d{2})\+?)"
    r"|(?P<days>\b(?P<d1>Mo|Tu|We|Th|Fr|Sa|Su)\b(?:\s*-\s*\b(?P<d2>Mo|Tu|We|Th|Fr|Sa|Su)\b)?)"
    r"|(?P<off>\b(?:off|closed)\b)"
    r"|(?P<ph>\b(?:PH|SH)\b)"
    r"|(?P<sep>[;,|\s])",
    re.IGNORECASE,
)


def hhmm_to_min(text):
    """'07:30' -> 450."""
    h, m = text.split(":")[:2]
    return int(h) * 60 + int(m)


def min_to_hhmm(minutes):
    """450 -> '07:30' (wraps past midnight)."""
    minutes = int(minutes) % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _day_span(d1, d2):
    """'Mo','Fr' -> [0..4]; wraps around for 'Fr-Mo'."""
    i = DAYS.index(d1.capitalize())
    j = DAYS.index((d2 or d1).capitalize())
    span = [i]
    while span[-1] != j:
        span.append((span[-1] + 1) % 7)
    return span


def _tokenize(text):
    """Return a list of tokens, or None if any part of the text is not understood."""
    tokens, pos = [], 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            return None  # unknown text, e.g. month names or "sunrise"
        pos = m.end()
        if m.group("sep"):
            continue
        tokens.append(m)
    return tokens


def _parse_schedule(text):
    """Parse text into a weekly schedule dict, or None if unparseable."""
    tokens = _tokenize(text)
    if not tokens:
        return None

    schedule = {d: [] for d in range(7)}
    current_days = None   # days selected by the rule being read
    rule_has_times = False
    reset_done = set()    # days already reset by the current rule
    skip_rule = False     # rule that only applies to public holidays

    for tok in tokens:
        if tok.group("days"):
            if rule_has_times or current_days is None:
                # a new day selector after times starts a new rule
                current_days, rule_has_times, reset_done, skip_rule = [], False, set(), False
            current_days += _day_span(tok.group("d1"), tok.group("d2"))
        elif tok.group("ph"):
            if rule_has_times or current_days is None:
                current_days, rule_has_times, reset_done = [], False, set()
            skip_rule = not current_days  # "PH off" alone -> ignore
        elif tok.group("range"):
            if skip_rule:
                continue
            days = current_days or list(range(7))
            start = int(tok.group("h1")) * 60 + int(tok.group("m1"))
            end = int(tok.group("h2")) * 60 + int(tok.group("m2"))
            if end <= start:
                end += 1440  # closes after midnight
            for d in days:
                # later rules replace earlier ones for the same day (OSM semantics)
                if d not in reset_done:
                    schedule[d] = []
                    reset_done.add(d)
                schedule[d].append((start, end))
            rule_has_times = True
        elif tok.group("off"):
            if not skip_rule:
                for d in current_days or list(range(7)):
                    schedule[d] = []
            rule_has_times = True
    if not any(schedule.values()):
        return None
    return schedule


def parse_opening_hours(text, default_opens="07:00", default_closes="23:00"):
    """
    Parse an OSM opening_hours string.

    Returns a dict:
        is_24x7, hours_verified, opens 'HH:MM', closes 'HH:MM',
        schedule {weekday: [(start_min, end_min), ...]}
    """
    text = (text or "").strip()
    if text.replace(" ", "") in ("24/7", "Mo-Su00:00-24:00", "00:00-24:00"):
        full = {d: [(0, 1440)] for d in range(7)}
        return {"is_24x7": True, "hours_verified": True,
                "opens": "00:00", "closes": "23:59", "schedule": full}

    schedule = _parse_schedule(text) if text else None
    if schedule is None:
        # unknown -> documented default from config, clearly flagged
        o, c = hhmm_to_min(default_opens), hhmm_to_min(default_closes)
        if c <= o:
            c += 1440
        return {"is_24x7": False, "hours_verified": False,
                "opens": default_opens, "closes": default_closes,
                "schedule": {d: [(o, c)] for d in range(7)}}

    is_24x7 = all(iv == [(0, 1440)] for iv in schedule.values())
    # Summary opens/closes for the restaurants table = the most common day pattern
    day_spans = [(iv[0][0], iv[-1][1]) for iv in schedule.values() if iv]
    opens, closes = Counter(day_spans).most_common(1)[0][0]
    return {"is_24x7": is_24x7, "hours_verified": True,
            "opens": min_to_hhmm(opens), "closes": min_to_hhmm(closes),
            "schedule": schedule}


def is_open(schedule, weekday, minute_of_day):
    """
    True if the place is open at minute_of_day (0..1439) on weekday (0 = Monday).

    Also checks yesterday's intervals that run past midnight, e.g. a place
    open Fri 18:00-02:00 is still open on Saturday at 01:30.
    """
    if not schedule:
        return False
    for start, end in schedule.get(weekday, ()):
        if start <= minute_of_day < end:
            return True
    yesterday = (weekday - 1) % 7
    for start, end in schedule.get(yesterday, ()):
        if end > 1440 and minute_of_day + 1440 < end:
            return True
    return False


def is_open_simple(opens, closes, minute_of_day, is_24x7=False):
    """
    Opening check using only the opens/closes summary columns.
    Handles closing after midnight (opens > closes), e.g. 18:00-02:00.
    """
    if is_24x7:
        return True
    o, c = hhmm_to_min(opens), hhmm_to_min(closes)
    if o <= c:
        return o <= minute_of_day < c
    return minute_of_day >= o or minute_of_day < c

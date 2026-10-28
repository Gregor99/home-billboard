# app.py - left 64x64: weather icon + today's low/high (Open-Meteo).
#          right 192x64: two scrolling calendar lines (today / tomorrow) from a Google
#          Calendar "secret address in iCal format".
# This is the file that is updated over Wi-Fi. Every config.py setting used here has a
# default, so an older config.py can never break an update.

import gc
import math
import time
import machine
import config

try:
    import urequests as requests
except ImportError:
    import requests

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={}&longitude={}"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    "&timezone={}&forecast_days=2"
)

SCROLL_X = 66      # scrolling area starts here (weather block is 0..63, divider at 64)
SCROLL_W = 190     # ... and runs to the right edge (256)
SCROLL_GAP = 48    # blank pixels between the end of the text and its repeat


def cfg(name, default):
    return getattr(config, name, default)


# ------------------------------------------------------------------------- HTTP
def open_url(url, feed, max_redirects=3):
    """GET a URL, following redirects by hand. Returns an open response (caller closes it)."""
    for _ in range(max_redirects + 1):
        feed()
        r = requests.get(url)
        status = r.status_code
        if status in (301, 302, 303, 307, 308):
            loc = None
            hdrs = getattr(r, "headers", None) or {}
            for k in hdrs:
                if k.lower() == "location":
                    loc = hdrs[k]
                    break
            r.close()
            if not loc:
                raise OSError("redirect without Location")
            url = loc.decode() if isinstance(loc, bytes) else loc
            continue
        if status != 200:
            r.close()
            raise OSError("HTTP %d" % status)
        return r
    raise OSError("too many redirects")


def http_get_json(url, feed):
    r = open_url(url, feed)
    try:
        return r.json()
    finally:
        r.close()


# ------------------------------------------------------------------------- time
def sync_time():
    """Set the board clock (UTC) from the internet. Returns True on success."""
    try:
        import ntptime
        ntptime.settime()
        return True
    except Exception as e:
        print("ntp: failed:", e)
        return False


def time_ok():
    return time.gmtime()[0] >= 2025


def days_from_civil(y, m, d):
    """Days since 1970-01-01 for a calendar date (works on any MicroPython epoch)."""
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def civil_from_days(z):
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    return y + (m <= 2), m, d


def days_in_month(y, m):
    if m == 12:
        return 31
    return days_from_civil(y, m + 1, 1) - days_from_civil(y, m, 1)


def local_minutes(offset_s):
    """Current local time as minutes since 1970-01-01 (local wall clock)."""
    t = time.gmtime()
    return days_from_civil(t[0], t[1], t[2]) * 1440 + t[3] * 60 + t[4] + offset_s // 60


# ------------------------------------------------------------------- weather data
def fetch_weather(wifi_connect, feed):
    if not wifi_connect():
        return None
    try:
        gc.collect()
        url = WEATHER_URL.format(config.LAT, config.LON, config.TIMEZONE.replace("/", "%2F"))
        j = http_get_json(url, feed)
        d = j["daily"]
        days = []
        for i in (0, 1):  # 0 = today, 1 = tomorrow
            days.append({
                "code": d["weather_code"][i],
                "tmax": d["temperature_2m_max"][i],
                "tmin": d["temperature_2m_min"][i],
                "pop": d["precipitation_probability_max"][i] or 0,
            })
        return {
            "days": days,
            "offset": j.get("utc_offset_seconds"),  # local UTC offset, follows summer time
        }
    except Exception as e:
        print("weather: fetch failed:", e)
        return None


def icon_kind(code):
    """Map WMO weather codes (used by Open-Meteo) to one of our icons."""
    if code in (0, 1):
        return "sun"
    if code == 2:
        return "partly"
    if code == 3:
        return "cloud"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "storm"
    return "cloud"


# ------------------------------------------------------------- iCal (.ics) reading
def ics_lines(raw, feed):
    """Yield unfolded iCal lines from a socket-like stream, reading 1 KB at a time."""
    buf = b""
    cur = None
    n = 0
    while True:
        chunk = raw.read(1024)
        if not chunk:
            break
        buf += chunk
        n += 1
        if n % 16 == 0:
            feed()
        while True:
            i = buf.find(b"\n")
            if i < 0:
                break
            line = buf[:i]
            buf = buf[i + 1:]
            if line.endswith(b"\r"):
                line = line[:-1]
            if line[:1] in (b" ", b"\t"):      # folded continuation line
                if cur is not None:
                    cur += line[1:]
                continue
            if cur is not None:
                yield _decode(cur)
            cur = line
    if buf:
        if buf[:1] in (b" ", b"\t") and cur is not None:
            cur += buf[1:].rstrip(b"\r")
        else:
            if cur is not None:
                yield _decode(cur)
            cur = buf.rstrip(b"\r")
    if cur is not None:
        yield _decode(cur)


def _decode(b):
    try:
        return b.decode("utf-8")
    except Exception:
        return "".join(chr(c) if c < 128 else "?" for c in b)


def parse_dt(params, value, offset_min):
    """iCal date/date-time -> (local minutes since 1970, all_day)."""
    v = value.strip()
    days = days_from_civil(int(v[0:4]), int(v[4:6]), int(v[6:8]))
    if len(v) == 8:
        return days * 1440, True
    m = days * 1440 + int(v[9:11]) * 60 + int(v[11:13])
    if v.endswith("Z"):
        m += offset_min   # UTC -> local. TZID times are taken as local time.
    return m, False


def parse_duration(v):
    """'PT1H30M', 'P1D', 'P2W' -> minutes."""
    total = 0
    num = ""
    sign = -1 if v.startswith("-") else 1
    for ch in v:
        if "0" <= ch <= "9":
            num += ch
        elif ch in "WDHMS" and num:
            total += int(num) * {"W": 10080, "D": 1440, "H": 60, "M": 1, "S": 0}[ch]
            num = ""
    return sign * total


WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _byday(s):
    """'WE' -> (0, 2); '2TU' -> (2, 1); '-1FR' -> (-1, 4)."""
    return (int(s[:-2]) if len(s) > 2 else 0), WEEKDAYS.index(s[-2:])


def _nth_ok(n, wd, D, y, m, d):
    if (D + 3) % 7 != wd:
        return False
    if n > 0:
        return (d - 1) // 7 + 1 == n
    if n < 0:
        return (days_in_month(y, m) - d) // 7 + 1 == -n
    return True


def expand_rrule(rule, start, dur, exdates, w0, w1, offset_min):
    """Start minutes of the occurrences of a recurring event that overlap [w0, w1)."""
    p = {}
    for part in rule.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            p[k.upper()] = v
    freq = p.get("FREQ", "")
    interval = int(p.get("INTERVAL", "1") or 1)
    count = int(p["COUNT"]) if "COUNT" in p else 0
    until = None
    if "UNTIL" in p:
        until, uday = parse_dt("", p["UNTIL"], offset_min)
        if uday:
            until += 1439
    byday = [_byday(x) for x in p["BYDAY"].split(",")] if "BYDAY" in p else None
    bymd = [int(x) for x in p["BYMONTHDAY"].split(",")] if "BYMONTHDAY" in p else None
    bymonth = [int(x) for x in p["BYMONTH"].split(",")] if "BYMONTH" in p else None
    wkst = WEEKDAYS.index(p.get("WKST", "MO"))

    sd = start // 1440
    tod = start % 1440
    sy, sm, sdd = civil_from_days(sd)
    sw = (sd + 3) % 7                       # weekday, Monday = 0
    wk0 = sd - (sd + 3 - wkst) % 7

    def matches(D):
        if D < sd:
            return False
        y, m, d = civil_from_days(D)
        wd = (D + 3) % 7
        if bymonth and m not in bymonth:
            return False
        if freq == "DAILY":
            if (D - sd) % interval:
                return False
            return not byday or any(w == wd for n, w in byday)
        if freq == "WEEKLY":
            if ((D - (D + 3 - wkst) % 7 - wk0) // 7) % interval:
                return False
            return wd in ([w for n, w in byday] if byday else [sw])
        if freq == "MONTHLY":
            if ((y - sy) * 12 + m - sm) % interval:
                return False
        elif freq == "YEARLY":
            if (y - sy) % interval:
                return False
            if not bymonth and m != sm:
                return False
        else:
            return D == sd
        if byday and not bymd:
            return any(_nth_ok(n, w, D, y, m, d) for n, w in byday)
        dim = days_in_month(y, m)
        return any((md if md > 0 else dim + md + 1) == d for md in (bymd or [sdd]))

    first = max(sd, (w0 - dur) // 1440)
    last = (w1 - 1) // 1440
    if last - first > 62:
        first = last - 62
    out = []
    for D in range(first, last + 1):
        if not matches(D):
            continue
        s = D * 1440 + tod
        if until is not None and s > until:
            break
        if count:
            seen = 0
            for D2 in range(sd, D):         # occurrences before this one
                if matches(D2):
                    seen += 1
                    if seen >= count:
                        break
            if seen >= count:
                break
        if s in exdates:
            continue
        if s < w1 and s + max(dur, 1) > w0:
            out.append(s)
    return out


def _unescape(s):
    return s.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_ics(lines, w0, w1, offset_min):
    """Read a whole .ics stream; return [(start, end, all_day, title)] overlapping [w0, w1)."""
    found = []          # (uid, start, end, all_day, title, is_master)
    moved = set()       # (uid, original start) of instances that were changed or cancelled
    ev = None
    sub = 0
    for line in lines:
        if line.startswith("BEGIN:"):
            if line == "BEGIN:VEVENT":
                ev = {"EXDATE": []}
                sub = 0
            elif ev is not None:
                sub += 1                    # e.g. VALARM inside the event
            continue
        if line.startswith("END:"):
            if line == "END:VEVENT" and ev is not None:
                try:
                    _handle_event(ev, found, moved, w0, w1, offset_min)
                except Exception as e:
                    print("ics: skipped event:", e)
                ev = None
            elif ev is not None and sub:
                sub -= 1
            continue
        if ev is None or sub or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name = head.split(";", 1)[0].upper()
        if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            ev[name] = (head, value)
        elif name == "EXDATE":
            ev["EXDATE"].append((head, value))
        elif name in ("SUMMARY", "RRULE", "UID", "STATUS", "DURATION"):
            ev[name] = value
    return [(s, e, a, t) for (u, s, e, a, t, master) in found
            if not (master and (u, s) in moved)]


def _handle_event(ev, found, moved, w0, w1, offset_min):
    if "DTSTART" not in ev:
        return
    uid = ev.get("UID", "")
    if "RECURRENCE-ID" in ev:
        rid = parse_dt(ev["RECURRENCE-ID"][0], ev["RECURRENCE-ID"][1], offset_min)[0]
        if w0 - 62 * 1440 <= rid < w1:
            moved.add((uid, rid))           # the original instance must not be shown
    if ev.get("STATUS", "").upper() == "CANCELLED":
        return
    start, all_day = parse_dt(ev["DTSTART"][0], ev["DTSTART"][1], offset_min)
    if "DTEND" in ev:
        end = parse_dt(ev["DTEND"][0], ev["DTEND"][1], offset_min)[0]
    elif "DURATION" in ev:
        end = start + parse_duration(ev["DURATION"])
    else:
        end = start + (1440 if all_day else 0)
    dur = max(end - start, 0)
    title = _unescape(ev.get("SUMMARY", "(no title)"))

    if "RRULE" in ev and "RECURRENCE-ID" not in ev:
        ex = set()
        for head, value in ev["EXDATE"]:
            for v in value.split(","):
                ex.add(parse_dt(head, v, offset_min)[0])
        for s in expand_rrule(ev["RRULE"], start, dur, ex, w0, w1, offset_min):
            found.append((uid, s, s + dur, all_day, title, True))
    elif start < w1 and max(end, start + 1) > w0:
        found.append((uid, start, end, all_day, title, False))


# ------------------------------------------------------------- calendar -> lines
FOLD = {"č": "c", "ć": "c", "š": "s", "ž": "z", "đ": "d",
        "Č": "C", "Ć": "C", "Š": "S", "Ž": "Z", "Đ": "D",
        "ä": "a", "ö": "o", "ü": "u", "Ä": "A", "Ö": "O", "Ü": "U",
        "ß": "ss", "é": "e", "è": "e", "á": "a", "à": "a",
        "í": "i", "ó": "o", "ú": "u"}


def clean_title(s, max_len):
    """Fold c-caron etc. to plain letters, drop emoji and other symbols the font lacks."""
    out = ""
    for c in s:
        if " " <= c <= "~":
            out += c
        else:
            out += FOLD.get(c, "")
    out = " ".join(out.split())
    if len(out) > max_len:
        out = out[:max_len - 2] + ".."
    return out


def person_of(title, people):
    """Index of the first person whose name starts the title, else len(people) (default colour)."""
    t = title.lower()
    for i, (name, _) in enumerate(people):
        if t.find(name.lower()) != -1:
            return i
    return len(people)


def to_entries(occ, w0, now, people, max_len):
    """Occurrences -> sorted [(day, time_text, colour_key, title)] for today (0) and tomorrow (1)."""
    rows = []
    for s, e, all_day, title in occ:
        e = max(e, s + 1)
        for day in (0, 1):
            ds = w0 + day * 1440
            if not (s < ds + 1440 and e > ds):
                continue
            if day == 0 and not all_day and e <= now:
                continue                    # already finished today
            if all_day or s < ds:
                tm, key = "", -1
            else:
                tm, key = "%d:%02d" % ((s % 1440) // 60, s % 60), s
            rows.append((day, key, tm, person_of(title, people), clean_title(title, max_len)))
    rows.sort(key=lambda r: (r[0], r[1], r[4]))
    return [(d, tm, c, t) for d, k, tm, c, t in rows]


def fetch_calendar(url, wifi_connect, feed, offset_s, people, max_len):
    """Returns the entry list, or None on failure (error is printed)."""
    if not wifi_connect():
        return None
    if not time_ok():
        print("calendar: clock not set yet")
        return None
    try:
        gc.collect()
        now = local_minutes(offset_s)
        w0 = (now // 1440) * 1440
        r = open_url(url, feed)
        try:
            occ = parse_ics(ics_lines(r.raw, feed), w0, w0 + 2 * 1440, offset_s // 60)
        finally:
            r.close()
        gc.collect()
        return to_entries(occ, w0, now, people, max_len)
    except Exception as e:
        print("calendar: fetch failed:", e)
        return None


# Segment colour keys: >= 0 colour index, -1 separator / hint text, -2 line label.
def build_lines(entries, labels=("TODAY", "TOMORROW"), empty="nothing planned"):
    lines = []
    for day in (0, 1):
        segs = [(labels[day] + ": ", -2)]
        count = 0
        for d, tm, key, title in entries:
            if d != day:
                continue
            if count:
                segs.append(("  |  ", -1))
            segs.append(((tm + " " + title) if tm else title, key))
            count += 1
        if not count:
            segs.append((empty, -1))
        lines.append(segs)
    return lines


def layout(g, segs, scale):
    """Measure each segment once. Returns ([(text, key, x, width)], total_width)."""
    items = []
    x = 0
    for text, key in segs:
        w = g.measure_text(text, scale)
        items.append((text, key, x, w))
        x += w
    return items, x


def to_rgb(c):
    if isinstance(c, str):
        h = c.lstrip("#")
        try:
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except Exception:
            return 255, 255, 255
    return c[0], c[1], c[2]


# ----------------------------------------------------------------------- drawing
class Icon:
    """Draws the icon designs (made on a 64 x 42 grid centred on 32,21) at any size and place."""

    def __init__(self, g, cx, cy, k):
        self.g, self.cx, self.cy, self.k = g, cx, cy, k

    def x(self, v):
        return self.cx + int(round((v - 32) * self.k))

    def y(self, v):
        return self.cy + int(round((v - 21) * self.k))

    def r(self, v):
        return max(1, int(round(v * self.k)))

    def circle(self, x, y, r):
        self.g.circle(self.x(x), self.y(y), self.r(r))

    def rect(self, x, y, w, h):
        self.g.rectangle(self.x(x), self.y(y), self.r(w), self.r(h))

    def line(self, x1, y1, x2, y2):
        self.g.line(self.x(x1), self.y(y1), self.x(x2), self.y(y2))

    def tri(self, x1, y1, x2, y2, x3, y3):
        self.g.triangle(self.x(x1), self.y(y1), self.x(x2), self.y(y2), self.x(x3), self.y(y3))


def draw_cloud(ic, cx, cy, pen):
    ic.g.set_pen(pen)
    ic.circle(cx - 9, cy + 2, 7)
    ic.circle(cx + 9, cy + 2, 7)
    ic.circle(cx, cy - 3, 9)
    ic.rect(cx - 9, cy + 2, 19, 8)


def draw_sun(ic, P, cx, cy, r):
    ic.g.set_pen(P["sun"])
    ic.circle(cx, cy, r)
    for i in range(8):
        a = i * math.pi / 4
        ic.line(cx + math.cos(a) * (r + 3), cy + math.sin(a) * (r + 3),
                cx + math.cos(a) * (r + 7), cy + math.sin(a) * (r + 7))


def draw_icon(ic, P, kind):
    g = ic.g
    if kind == "sun":
        draw_sun(ic, P, 32, 21, 9)
    elif kind == "partly":
        draw_sun(ic, P, 22, 13, 6)
        draw_cloud(ic, 35, 26, P["cloud"])
    elif kind == "cloud":
        draw_cloud(ic, 40, 15, P["cloud_dark"])
        draw_cloud(ic, 30, 24, P["cloud"])
    elif kind == "fog":
        draw_cloud(ic, 32, 12, P["cloud_dark"])
        g.set_pen(P["fog"])
        for i, y in enumerate((26, 31, 36)):
            ic.rect(14 + (i % 2) * 4, y, 36, 2)
    elif kind == "rain":
        draw_cloud(ic, 32, 14, P["cloud_dark"])
        g.set_pen(P["rain"])
        for x in (23, 31, 39):
            ic.line(x, 27, x - 2, 33)
            ic.line(x + 1, 27, x - 1, 33)
    elif kind == "snow":
        draw_cloud(ic, 32, 14, P["cloud_dark"])
        g.set_pen(P["snow"])
        for x, y in ((24, 28), (32, 30), (40, 28), (28, 35), (36, 35)):
            ic.circle(x, y, 1)
    elif kind == "storm":
        draw_cloud(ic, 32, 13, P["cloud_dark"])
        g.set_pen(P["bolt"])
        ic.tri(35, 22, 27, 33, 33, 33)
        ic.tri(33, 30, 39, 30, 29, 41)


ICON_W = 29     # width of the icon area in each weather row; temperatures use the rest


def draw_day(g, P, day, top):
    """One 64 x 32 weather row: icon on the left, high (orange) over low (blue) on the right."""
    draw_icon(Icon(g, ICON_W // 2, top + 15, 0.6), P, icon_kind(day["code"]))
    hi = str(round(day["tmax"]))
    lo = str(round(day["tmin"]))
    room = 64 - ICON_W
    scale = 2
    if max(g.measure_text(hi, 2), g.measure_text(lo, 2)) > room:
        scale = 1
    h = 8 * scale
    y_hi = top + 1 if scale == 2 else top + 6
    y_lo = y_hi + h if scale == 2 else y_hi + h + 4
    g.set_pen(P["orange"])
    g.text(hi, 64 - g.measure_text(hi, scale), y_hi, 200, scale)
    g.set_pen(P["blue"])
    g.text(lo, 64 - g.measure_text(lo, scale), y_lo, 200, scale)
    if day["pop"] >= 50:  # small raindrop bottom-left = rain likely that day
        g.set_pen(P["rain"])
        g.circle(3, top + 28, 2)
        g.triangle(3, top + 23, 1, top + 27, 5, top + 27)


def draw_weather(g, P, weather, stale):
    g.remove_clip()
    g.set_pen(P["black"])
    g.rectangle(0, 0, 64, 64)
    if weather:
        draw_day(g, P, weather["days"][0], 0)    # today
        draw_day(g, P, weather["days"][1], 32)   # tomorrow
    else:
        g.set_pen(P["white"])
        g.text("...", 20, 24, 200, 2)
    g.set_pen(P["dim"])
    g.line(0, 31, 63, 31)
    if stale:  # red dot (top-left) = showing old weather, last refresh failed
        g.set_pen(P["red"])
        g.rectangle(0, 0, 3, 3)


def draw_calendar(g, P, ppens, layouts, pos, dt, speed, scale, text_y, stale):
    """Redraw the right 192x64 block: two lines, each scrolling if it is wider than the area."""
    g.remove_clip()
    g.set_pen(P["black"])
    g.rectangle(64, 0, 192, 64)

    for i in range(2):
        items, total = layouts[i]
        if total > SCROLL_W:
            cycle = total + SCROLL_GAP
            pos[i] += dt * speed / 1000
            if pos[i] >= cycle:
                pos[i] %= cycle
            shift = int(pos[i])
            copies = (0, cycle)
        else:
            shift = 0
            copies = (0,)
        base = SCROLL_X - shift
        g.set_clip(SCROLL_X, i * 32, SCROLL_W, 32)
        for c in copies:
            for text, key, ix, w in items:
                x = base + c + ix
                if x >= SCROLL_X + SCROLL_W or x + w <= SCROLL_X:
                    continue  # off-screen: skip the draw call
                if key >= 0:
                    pen = ppens[key] if key < len(ppens) else P["white"]
                elif key == -2:
                    pen = P["label"]
                else:
                    pen = P["grey"]
                g.set_pen(pen)
                g.text(text, x, text_y[i], 2000, scale)
    g.remove_clip()

    g.set_pen(P["dim"])
    g.line(64, 0, 64, 63)
    g.line(SCROLL_X, 31, 255, 31)
    if stale:  # amber dot (top-right) = showing old calendar data, last refresh failed
        g.set_pen(P["amber"])
        g.rectangle(252, 1, 3, 3)


# ------------------------------------------------------------------------- main
def run(i75, check_update, mark_ok, wifi_connect, feed):
    from interstate75 import Interstate75

    g = i75.display
    g.set_font("bitmap8")
    P = {
        "black": g.create_pen(0, 0, 0),
        "white": g.create_pen(255, 255, 255),
        "sun": g.create_pen(255, 200, 0),
        "cloud": g.create_pen(205, 210, 220),
        "cloud_dark": g.create_pen(120, 130, 145),
        "fog": g.create_pen(150, 160, 170),
        "rain": g.create_pen(60, 140, 255),
        "snow": g.create_pen(255, 255, 255),
        "bolt": g.create_pen(255, 220, 0),
        "blue": g.create_pen(80, 160, 255),
        "orange": g.create_pen(255, 140, 0),
        "red": g.create_pen(255, 0, 0),
        "amber": g.create_pen(255, 120, 0),
        "label": g.create_pen(190, 190, 190),
        "grey": g.create_pen(110, 110, 110),
        "dim": g.create_pen(40, 40, 40),
    }

    cal_url = cfg("CALENDAR_ICS_URL", "")
    cal_every = cfg("CALENDAR_REFRESH_MIN", 15) * 60000
    people = cfg("PEOPLE", [])
    max_len = cfg("MAX_TITLE", 26)
    scale = cfg("TEXT_SCALE", 2)
    speed = cfg("SCROLL_PX_PER_SEC", 36)
    labels = cfg("LABELS", ("TODAY", "TOMORROW"))
    empty = cfg("EMPTY_TEXT", "nothing planned")
    text_y = [(32 - 8 * scale) // 2, 32 + (32 - 8 * scale) // 2]

    # Colour per person (title starts with their name), then the default colour.
    ppens = [g.create_pen(*to_rgb(c)) for _, c in people]
    ppens.append(g.create_pen(*to_rgb(cfg("DEFAULT_COLOR", (255, 255, 255)))))

    offset_s = cfg("UTC_OFFSET", 3600)   # replaced by the weather service's value
    weather = None
    weather_stale = False
    weather_dirty = True
    have_cal = False
    cal_stale = False
    pos = [0.0, 0.0]
    first = [("loading...", -1)] if cal_url else [("Set CALENDAR_ICS_URL in config.py", -1)]
    layouts = [layout(g, first, scale), layout(g, [], scale)]

    start = time.ticks_ms()
    next_ntp = start
    next_weather = start
    next_cal = start
    next_update = time.ticks_add(start, 20000)  # first update check shortly after boot
    btn_block = start
    last = start
    marked = False

    def due(deadline):
        return time.ticks_diff(time.ticks_ms(), deadline) >= 0

    while True:
        feed()

        if due(next_ntp):
            ok = wifi_connect() and sync_time()
            next_ntp = time.ticks_add(time.ticks_ms(), (24 * 3600 if ok else 300) * 1000)

        if due(next_weather):
            data = fetch_weather(wifi_connect, feed)
            if data:
                weather, weather_stale = data, False
                if data.get("offset") is not None:
                    offset_s = data["offset"]
                next_weather = time.ticks_add(time.ticks_ms(), config.WEATHER_REFRESH_MIN * 60000)
            else:
                weather_stale = weather is not None
                next_weather = time.ticks_add(time.ticks_ms(), 120000)  # retry in 2 min
            weather_dirty = True

        if cal_url and due(next_cal):
            entries = fetch_calendar(cal_url, wifi_connect, feed, offset_s, people, max_len)
            if entries is not None:
                have_cal, cal_stale = True, False
                lines = build_lines(entries, labels, empty)
                layouts = [layout(g, lines[0], scale), layout(g, lines[1], scale)]
                next_cal = time.ticks_add(time.ticks_ms(), cal_every)
            else:
                cal_stale = have_cal
                if not have_cal:
                    msg = "calendar offline" if time_ok() else "waiting for clock..."
                    layouts = [layout(g, [(msg, -1)], scale), layout(g, [], scale)]
                next_cal = time.ticks_add(time.ticks_ms(), 60000)  # retry in 1 min
            last = time.ticks_ms()  # the download must not count as scrolling time

        if weather_dirty:
            draw_weather(g, P, weather, weather_stale)
            weather_dirty = False

        now = time.ticks_ms()
        dt = min(time.ticks_diff(now, last), 100)
        last = now
        draw_calendar(g, P, ppens, layouts, pos, dt, speed, scale, text_y, cal_stale)
        i75.update()

        # The new code counts as good once it has run for 15 seconds without crashing.
        if not marked and time.ticks_diff(now, start) > 15000:
            mark_ok()
            marked = True

        pressed = False
        if time.ticks_diff(now, btn_block) >= 0:
            try:
                pressed = i75.switch_pressed(Interstate75.SWITCH_A)
            except Exception:
                pass

        if pressed or due(next_update):
            btn_block = time.ticks_add(time.ticks_ms(), 3000)  # ignore a held button
            next_update = time.ticks_add(time.ticks_ms(), config.UPDATE_CHECK_HOURS * 3600000)
            if check_update():
                machine.reset()  # new code installed: restart into it
            last = time.ticks_ms()

        time.sleep_ms(10)

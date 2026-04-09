#!/usr/bin/env python3
"""
Sync events from the Luma calendar ICS feed into ALL_EVENTS in index.html.

- Fetches the ICS feed.
- Parses each VEVENT into { name, date, location, url } (url = public luma slug when
  derivable from the DESCRIPTION field, otherwise the evt- id).
- Merges with the existing ALL_EVENTS JS array in index.html:
    - Existing entries are matched by date + url (or date + name fallback) and
      have name / date / location refreshed from Luma. Other fields
      (cover_url, hosts, guests, type, compact) are preserved.
    - New entries are added as stubs. Defaults: type = "In-Person" if location
      looks physical, else "Online Fireside".
- Events are sorted by date descending and the ALL_EVENTS array in index.html
  is rewritten in place between the `const ALL_EVENTS = [` and matching `];`
  markers.

Run: python3 scripts/sync-luma.py
Env:
  LUMA_ICS_URL (optional) — override ICS URL.
"""
from __future__ import annotations
import json
import os
import re
import sys
import subprocess
from pathlib import Path

ICS_URL = os.environ.get(
    "LUMA_ICS_URL",
    "https://api.lu.ma/ics/get?entity=calendar&id=cal-KC7HVRLO3J9jxZD",
)
ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"


def fetch_ics(url: str) -> str:
    out = subprocess.check_output(["curl", "-sL", "--fail", url], timeout=30)
    return out.decode("utf-8")


def unfold(ics: str) -> list[str]:
    """Undo RFC5545 line folding (a line continuing on the next starts with space/tab)."""
    out: list[str] = []
    for line in ics.splitlines():
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_ics(ics: str) -> list[dict]:
    events: list[dict] = []
    cur: dict | None = None
    for line in unfold(ics):
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        # key[;params]:value
        m = re.match(r"^([A-Z\-]+)(?:;[^:]*)?:(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        val = val.replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n").replace("\\N", "\n")
        cur[key] = val
    return events


def ics_to_row(ev: dict) -> dict | None:
    dtstart = ev.get("DTSTART")
    if not dtstart:
        return None
    # YYYYMMDD or YYYYMMDDTHHMMSSZ
    m = re.match(r"^(\d{4})(\d{2})(\d{2})", dtstart)
    if not m:
        return None
    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    name = (ev.get("SUMMARY") or "").strip()
    desc = ev.get("DESCRIPTION") or ""
    loc = (ev.get("LOCATION") or "").strip()

    # Prefer the public slug from "Get up-to-date information at: https://luma.com/<slug>"
    url = None
    m2 = re.search(r"https?://(?:lu\.ma|luma\.com)/([A-Za-z0-9\-]+)", desc)
    if m2:
        url = m2.group(1)
    else:
        m3 = re.search(r"luma\.com/event/(evt-[A-Za-z0-9]+)", loc)
        if m3:
            url = m3.group(1)

    # Guess location
    location_guess = "Online"
    low = name.lower()
    if "austin" in low or "[austin" in low:
        location_guess = "Austin, TX"
    elif "nyc" in low or "new york" in low:
        location_guess = "New York, NY"
    elif "sf" in low or "san francisco" in low:
        location_guess = "San Francisco, CA"
    elif "[online]" in low or "online" in loc.lower():
        location_guess = "Online"

    type_guess = "In-Person" if location_guess != "Online" else "Online Fireside"

    row = {
        "name": name,
        "date": date,
        "location": location_guess,
        "url": url or "",
        "type": type_guess,
    }
    return row


# ---- index.html surgery ----

JS_START = "const ALL_EVENTS = ["
JS_END_MARKER = "];"


def extract_events_block(html: str) -> tuple[int, int, str]:
    start = html.find(JS_START)
    if start == -1:
        raise SystemExit("Could not find ALL_EVENTS in index.html")
    # find the matching closing `];` after start
    depth = 0
    i = start + len(JS_START) - 1  # position at the `[`
    while i < len(html):
        c = html[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                # expect `];`
                end = i + 1
                # include the `;`
                if html[end : end + 1] == ";":
                    end += 1
                return start, end, html[start:end]
        i += 1
    raise SystemExit("Unterminated ALL_EVENTS array")


# Very small JS-object literal parser (just enough for this file)
JS_KEY_RE = re.compile(r'(\w+)\s*:\s*')


def parse_js_array(block: str) -> list[dict]:
    """Parse the JS array literal of object entries. Supports strings (single/double),
    numbers, booleans, unquoted keys, trailing commas, // line comments."""
    # strip the leading `const ALL_EVENTS = [` and trailing `];`
    inner = block[block.index("[") + 1 : block.rindex("]")]
    # Remove // line comments (only when // is at line start after whitespace,
    # not inside a URL like https://)
    inner = re.sub(r"(?m)^\s*//[^\n]*", "", inner)

    out: list[dict] = []
    i = 0
    n = len(inner)

    def skip_ws(j: int) -> int:
        while j < n and inner[j] in " \t\r\n,":
            j += 1
        return j

    def read_string(j: int) -> tuple[str, int]:
        quote = inner[j]
        j += 1
        buf = []
        while j < n:
            ch = inner[j]
            if ch == "\\" and j + 1 < n:
                nxt = inner[j + 1]
                if nxt == "n":
                    buf.append("\n")
                elif nxt == "t":
                    buf.append("\t")
                elif nxt == "u":
                    buf.append(chr(int(inner[j + 2 : j + 6], 16)))
                    j += 6
                    continue
                else:
                    buf.append(nxt)
                j += 2
                continue
            if ch == quote:
                return "".join(buf), j + 1
            buf.append(ch)
            j += 1
        raise ValueError("Unterminated string")

    def read_value(j: int) -> tuple[object, int]:
        j = skip_ws(j)
        ch = inner[j]
        if ch in "\"'":
            return read_string(j)
        if ch == "{":
            return read_object(j)
        # number/bool/null/identifier
        m = re.match(r"(-?\d+(?:\.\d+)?|true|false|null)", inner[j:])
        if not m:
            raise ValueError(f"Unexpected value at {j}: {inner[j:j+20]!r}")
        tok = m.group(1)
        j += len(tok)
        if tok == "true":
            return True, j
        if tok == "false":
            return False, j
        if tok == "null":
            return None, j
        if "." in tok:
            return float(tok), j
        return int(tok), j

    def read_object(j: int) -> tuple[dict, int]:
        assert inner[j] == "{"
        j += 1
        obj: dict = {}
        while True:
            j = skip_ws(j)
            if j >= n:
                raise ValueError("Unterminated object")
            if inner[j] == "}":
                return obj, j + 1
            # key
            if inner[j] in "\"'":
                key, j = read_string(j)
            else:
                m = re.match(r"(\w+)", inner[j:])
                if not m:
                    raise ValueError(f"Bad key at {j}")
                key = m.group(1)
                j += len(key)
            j = skip_ws(j)
            assert inner[j] == ":", f"expected : got {inner[j]!r}"
            j += 1
            val, j = read_value(j)
            obj[key] = val

    i = skip_ws(i)
    while i < n:
        if inner[i] != "{":
            break
        obj, i = read_object(i)
        out.append(obj)
        i = skip_ws(i)
    return out


def js_string(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def render_event(ev: dict) -> str:
    # Stable field order
    order = ["name", "date", "location", "url", "cover_url", "hosts", "guests", "type", "compact"]
    parts = []
    for k in order:
        if k not in ev:
            continue
        v = ev[k]
        if isinstance(v, str):
            parts.append(f"{k}: {js_string(v)}")
        elif isinstance(v, bool):
            parts.append(f"{k}: {'true' if v else 'false'}")
        elif v is None:
            parts.append(f"{k}: null")
        else:
            parts.append(f"{k}: {v}")
    # Any extra keys not in order
    for k, v in ev.items():
        if k in order:
            continue
        if isinstance(v, str):
            parts.append(f"{k}: {js_string(v)}")
        else:
            parts.append(f"{k}: {json.dumps(v)}")
    return "  { " + ", ".join(parts) + " },"


def render_all_events(events: list[dict]) -> str:
    # Sort date desc
    events = sorted(events, key=lambda e: e.get("date", ""), reverse=True)
    lines = [JS_START]
    current_year = None
    for ev in events:
        year = ev.get("date", "")[:4]
        if year != current_year:
            lines.append(f"\n  // ——— {year} ———")
            current_year = year
        lines.append(render_event(ev))
    lines.append("];")
    return "\n".join(lines)


def merge(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int, int]:
    by_key: dict[tuple, dict] = {}
    for e in existing:
        by_key[(e.get("date"), e.get("url") or e.get("name"))] = e

    added = 0
    updated = 0
    for inc in incoming:
        k = (inc["date"], inc.get("url") or inc["name"])
        if k in by_key:
            tgt = by_key[k]
            changed = False
            # Only refresh the name from Luma; leave location/type alone on
            # existing entries so we don't clobber curated values with guesses.
            for field in ("name", "url"):
                new_val = inc.get(field)
                if new_val and tgt.get(field) != new_val:
                    tgt[field] = new_val
                    changed = True
            if changed:
                updated += 1
        else:
            by_key[k] = dict(inc)
            added += 1
    return list(by_key.values()), added, updated


def main() -> int:
    ics = fetch_ics(ICS_URL)
    raw = parse_ics(ics)
    incoming = [r for r in (ics_to_row(e) for e in raw) if r]
    print(f"Fetched {len(incoming)} events from Luma")

    html = INDEX.read_text()
    start, end, block = extract_events_block(html)
    existing = parse_js_array(block)
    print(f"Found {len(existing)} existing events in index.html")

    merged, added, updated = merge(existing, incoming)
    print(f"Added {added} new events, updated {updated}")

    new_block = render_all_events(merged)
    new_html = html[:start] + new_block + html[end:]
    INDEX.write_text(new_html)
    print(f"Wrote {len(merged)} events back to index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())

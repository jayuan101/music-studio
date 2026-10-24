"""Built-in "Library health" lists: smart-playlist rules for the things worth
fixing, shown in the Library's source list with live counts.

Each is an ordinary rule set (core/playlist_rules.py) with a fixed negative
id, so it is never stored in the database. Open one and the Library's own
bulk buttons -- "Update all artwork", "Fix all metadata", and so on -- act on
exactly the songs that list shows, which turns each preset into a work queue.
"""

from __future__ import annotations


def _rule(field: str, op: str = "is_false") -> dict:
    return {"match": "all", "rules": [{"field": field, "op": op}], "order_by": "albumartist"}


#: (id, name, rules). Ids are negative and never change: they are how the UI
#: and Library.get_playlist() tell a preset from a saved playlist.
PRESETS: tuple[tuple[int, str, dict], ...] = (
    (-1, "Missing artwork", _rule("has_artwork")),
    (-2, "Missing album", _rule("has_album")),
    (-3, "Missing year", _rule("has_year")),
    (-4, "No ReplayGain", _rule("has_replaygain")),
    (-5, "Lossy files", _rule("is_lossless")),
    (-6, "Looks transcoded", _rule("suspect_lossless", "is_true")),
    (-7, "Lossless, not checked yet", _rule("lossless_unchecked", "is_true")),
)

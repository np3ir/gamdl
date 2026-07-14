TEMP_PATH_TEMPLATE = "gamdl_temp_{}"
ILLEGAL_CHARS_RE = r'[\\/:*?"<>|;]'
ILLEGAL_CHAR_REPLACEMENT = "_"

# Unicode dash/hyphen lookalikes -> ASCII hyphen. Apple Music (like Tidal etc.)
# sometimes uses these instead of "-"; normalizing keeps release names identical
# across sources/tools (matches tiddl). Covers hyphen, non-breaking hyphen,
# figure/en/em dash, horizontal bar and minus sign.
DASH_TO_HYPHEN = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
})

# Full-width Unicode replacements for forbidden Windows filename characters.
# Used by _sanitize_string when use_fullwidth_replacements=True (default).
FULLWIDTH_REPLACEMENTS = {
    "\\": "＼",
    "/": "／",
    ":": "：",
    "*": "＊",
    "?": "？",
    '"': "＂",
    "<": "＜",
    ">": "＞",
    "|": "｜",
    ";": "；",
    "[": "(",
    "]": ")",
}

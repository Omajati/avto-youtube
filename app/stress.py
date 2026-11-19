"""Stress marks for the voice-over: a dictionary of words where the voice puts the stress wrongly.

The dictionary is plain text, one word per line, the stressed vowel marked:
    сунду́чком          — the acute accent after the vowel (copy it from here: ́ )
    сундУчком           — or the stressed vowel as a capital letter
    И́рмино             — names: only the accent sign (a capital first letter is just a name)
    сундУч*             — a star at the end: every word that starts like this (сундучок, сундучком, сундучка…)
    # comment
Before the text goes to the voice service the program marks those words (the way the owner chose: an accent
sign or a capital vowel); the word timings that come back are cleaned, so subtitles and scene binding see the
ordinary text."""
import json
import re
from pathlib import Path

ACCENT = "́"
VOWELS = "аеёиоуыэюяaeiouy"
MODES = ("accent", "capital")


def _entry(line: str):
    """(stem without marks, index of the stressed vowel in it, star) or None."""
    word = line.strip().split("#", 1)[0].strip()
    if not word:
        return None
    word = re.split(r"\s*(?:→|->|=|—|\s)\s*", word)[-1].strip()  # «сундучком → сунду́чком» or just «сунду́чком»
    star = word.endswith("*")
    word = word.rstrip("*")
    plain, index = "", None
    for ch in word:
        if ch == ACCENT:
            if plain and index is None:
                index = len(plain) - 1
            continue
        if ch.isupper() and ch.lower() in VOWELS and index is None and plain:
            index = len(plain)
        plain += ch.lower()
    if index is None or not re.fullmatch(r"[^\W\d_]+(?:-[^\W\d_]+)*", plain):
        return None
    return plain.replace("ё", "е"), index, star


def parse(*texts: str) -> list:
    rules = {}
    for text in texts:
        for line in (text or "").splitlines():
            e = _entry(line)
            if e:
                rules[(e[0], e[2])] = e
    # longer stems first, whole words before stars
    return sorted(rules.values(), key=lambda e: (-len(e[0]), e[2]))


def _mark(word: str, index: int, mode: str) -> str:
    if ACCENT in word or any(c.isupper() for c in word[1:]):
        return word  # already marked by the scriptwriter
    if mode == "capital":
        return word[:index] + word[index].upper() + word[index + 1:]
    return word[:index + 1] + ACCENT + word[index + 1:]


def apply(text: str, rules: list, mode: str = "accent") -> str:
    if not rules or not text:
        return text

    def one(m):
        word = m.group(0)
        low = word.lower().replace("ё", "е")
        for stem, index, star in rules:
            if (low == stem or (star and low.startswith(stem))) and index < len(word):
                return _mark(word, index, mode)
        return word

    return re.sub(r"[^\W\d_]+(?:-[^\W\d_]+)*" + ACCENT + "?[^\\W\\d_]*", one, text)


def clean_alignment(path: Path, original: str, sent: str):
    """Word timings of the voice service: give them back the original letters (no marks)."""
    try:
        a = json.loads(path.read_text("utf-8"))
        chars = a["characters"]
        st, en = a["character_start_times_seconds"], a["character_end_times_seconds"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    if "".join(chars) == sent:
        keep = [i for i, c in enumerate(chars) if c != ACCENT]
        chars, st, en = [chars[i] for i in keep], [st[i] for i in keep], [en[i] for i in keep]
        if len(chars) == len(original):
            chars = list(original)
    else:
        keep = [i for i, c in enumerate(chars) if c != ACCENT]
        chars, st, en = [chars[i] for i in keep], [st[i] for i in keep], [en[i] for i in keep]
        out, prev = [], " "
        for c in chars:  # a capital vowel inside a word was a stress mark
            out.append(c.lower() if c.isupper() and c.lower() in VOWELS and prev.isalpha() and not prev.isupper() else c)
            prev = c
        chars = out
    a.update(characters=chars, character_start_times_seconds=st, character_end_times_seconds=en)
    path.write_text(json.dumps(a), encoding="utf-8")

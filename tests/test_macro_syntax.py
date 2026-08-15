"""Validates every macros/*.cfg gcode_macro's `gcode:` template compiles
cleanly, using the *exact* preprocessing and Jinja2 delimiters Klipper
itself uses - not plain jinja2.Environment() defaults, which do NOT
reproduce this and would miss real bugs.

This exists because of a real bug found live (see docs/PROTOCOL.md /
FINDINGS.md, 2026-08-16): Klipper's config loader strips everything after
the *first* `#` on every raw line - including inside a multi-line `gcode:`
value - before Jinja2 ever sees it. A Jinja2 `{# comment #}`'s own `#`
characters trip this, silently truncating the line and producing a
confusing `jinja2.exceptions.TemplateSyntaxError: unexpected 'end of
template'` that points nowhere near the real cause. Two of this repo's own
macro drafts hit this before it was caught live, at the printer, on a
real Klipper restart - this test exists so nobody has to rediscover it
that way again.

Klipper's own custom Jinja2 delimiters (block '{% %}', variable '{ }' -
NOT the Jinja2 default '{{ }}') are replicated here to match
klippy/extras/gcode_macro.py's `jinja2.Environment('{%', '%}', '{', '}')`
exactly.
"""
import configparser
import glob
import os

import jinja2
import pytest

MACROS_DIR = os.path.join(os.path.dirname(__file__), "..", "macros")


def _klipper_style_strip_comments(text):
    """Replicates configfile.py's _parse_config(): strip everything from
    the first '#' onward, on every raw line, before configparser ever
    sees it. This is what makes Jinja2 '{# #}' comments dangerous inside
    a gcode: block - see this module's docstring."""
    lines = []
    for line in text.split("\n"):
        pos = line.find("#")
        if pos >= 0:
            line = line[:pos]
        lines.append(line)
    return "\n".join(lines)


def _macro_cfg_files():
    return sorted(glob.glob(os.path.join(MACROS_DIR, "*.cfg")))


@pytest.mark.parametrize("cfg_path", _macro_cfg_files(),
                          ids=lambda p: os.path.basename(p))
def test_all_gcode_macro_templates_compile(cfg_path):
    with open(cfg_path, encoding="utf-8") as f:
        raw = f.read()

    stripped = _klipper_style_strip_comments(raw)
    parser = configparser.RawConfigParser(strict=False,
                                           inline_comment_prefixes=(";", "#"))
    parser.read_string(stripped, source=cfg_path)

    # Klipper's own delimiters: block '{% %}', variable '{ }' (not '{{ }}').
    env = jinja2.Environment("{%", "%}", "{", "}")

    macro_sections = [s for s in parser.sections()
                       if s.startswith("gcode_macro ")]
    assert macro_sections, "%s defines no [gcode_macro ...] sections" % cfg_path

    for section in macro_sections:
        if not parser.has_option(section, "gcode"):
            continue
        gcode = parser.get(section, "gcode")
        try:
            env.from_string(gcode)
        except jinja2.TemplateSyntaxError as e:
            pytest.fail(
                "%s: [%s]'s gcode: template fails to compile under "
                "Klipper's real preprocessing/delimiters: %s\n"
                "(common cause: a Jinja2 '{# ... #}' comment inside the "
                "gcode: block - Klipper's own '#'-stripping mangles it, "
                "see this test file's docstring)" % (cfg_path, section, e))

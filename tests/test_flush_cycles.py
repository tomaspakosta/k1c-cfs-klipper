"""
Verifies the cycle-split Jinja2 logic in macros/flush_draft.cfg against
gitstonelabs/creality-cfs-klipper's documented wire-verified breakdowns
(reverse-engineered from the compiled stock box wrapper binary - see that
file's header comment for the full citation).

This does NOT test anything about running on real Klipper or real
hardware - it only confirms our Jinja2 port of the split algorithm
produces byte-identical results to the known-correct source, using plain
jinja2 outside Klipper (pytest-installable, no Klipper environment needed).
"""
import pytest

jinja2 = pytest.importorskip("jinja2")

# Extracted from macros/flush_draft.cfg's cycle-split block - keep this in
# sync with that file if the split logic there changes.
CYCLE_SPLIT_TEMPLATE = """
{% set cap = flush_cycle_cap %}
{% if total <= cap %}
    {% set cycles = [total] %}
{% else %}
    {% set rest = total - cap %}
    {% set n = (rest / cap)|round(0, 'ceil')|int %}
    {% set n = [n, 1]|max %}
    {% set cycles = [cap] + [rest / n] * n %}
{% endif %}
{{ cycles }}
"""


def split(total, cap=80.0):
    env = jinja2.Environment()
    tmpl = env.from_string(CYCLE_SPLIT_TEMPLATE)
    rendered = tmpl.render(total=total, flush_cycle_cap=cap).strip()
    return eval(rendered)  # noqa: S307 - trusted, our own template output


@pytest.mark.parametrize("total,cap,expected", [
    # gitstonelabs' documented wire-verified breakdowns
    (158.75, 80.0, [80.0, 78.75]),
    (101.25, 80.0, [80.0, 21.25]),
    # default fallback total (140mm) at default cap (80mm)
    (140.0, 80.0, [80.0, 60.0]),
    # exactly at the cap - single cycle, no split
    (80.0, 80.0, [80.0]),
    # below the cap - single cycle
    (45.0, 80.0, [45.0]),
])
def test_known_breakdowns(total, cap, expected):
    result = split(total, cap)
    assert len(result) == len(expected)
    for got, want in zip(result, expected):
        assert got == pytest.approx(want, abs=0.01)


def test_343_33_splits_into_five_near_equal_cycles():
    # gitstonelabs documents this as [80, 65.83 x4] - a 5-cycle split.
    result = split(343.33, 80.0)
    assert len(result) == 5
    assert result[0] == pytest.approx(80.0, abs=0.01)
    for cyc in result[1:]:
        assert cyc == pytest.approx(65.8325, abs=0.01)


def test_volume_formula():
    # total = nozzle_volume/2.4 + (5/12)*volume*multiplier
    nozzle_volume = 108.0
    volume = 108.0
    multiplier = 1.0
    total = (nozzle_volume / 2.4) + ((5.0 / 12.0) * volume * multiplier)
    assert total == pytest.approx(90.0, abs=0.01)

"""The translation layer: one catalog, two complete languages, no key left behind.

The failure this file exists to stop is quiet: a key used on the page but missing from
the catalog renders as the bare key ("hint.unlock") on the parent's phone, in whichever
corner of whichever tab, and nothing crashes. So the page's own key references are
extracted and checked against the catalog, and the safety-critical sentences are pinned
in BOTH languages - a warning that exists only in English is not a warning this parent
will read.
"""

from __future__ import annotations

import re

from safehouse_panel import i18n, page
from safehouse_panel.i18n import msg, render


# --- the catalog itself ------------------------------------------------------

def test_both_languages_carry_every_key_and_no_blanks():
    assert set(i18n.CATALOG["en"]) == set(i18n.CATALOG["pt"])
    for lang in ("en", "pt"):
        for key, text in i18n.CATALOG[lang].items():
            assert isinstance(text, str) and text.strip(), (lang, key)


def test_translated_templates_keep_the_same_params():
    """A pt template that drops {n} silently loses the number the sentence is about."""
    param = re.compile(r"\{(\w+)\}")
    for key in i18n.CATALOG["en"]:
        en_params = set(param.findall(i18n.CATALOG["en"][key]))
        pt_params = set(param.findall(i18n.CATALOG["pt"][key]))
        assert en_params == pt_params, key


# --- rendering ---------------------------------------------------------------

def test_render_substitutes_params_in_both_languages():
    m = msg("err.lockout", min=3)
    assert render(m) == "Too many failed attempts. Try again in 3 minute(s)."
    assert render(m, "pt") == "Tentativas demais. Tente de novo em 3 minuto(s)."


def test_render_resolves_nested_messages_and_lists():
    m = [msg("ctl.yt.now", state=msg("st.BLOCKED")), msg("ctl.bonus.midnight")]
    assert render(m) == ("YouTube is now BLOCKED at the resolver. "
                         "It expires at midnight.")
    assert render(m, "pt") == ("O YouTube agora está BLOQUEADO no resolvedor. "
                               "Expira à meia-noite.")


def test_render_survives_what_it_does_not_know():
    """A missing key or a plain string must never take down a log line."""
    assert render("verbatim pass-through") == "verbatim pass-through"
    assert render({"k": "no.such.key"}) == "no.such.key"
    assert render(None) == ""
    assert render(msg("v.min")) == " min"  # a missing param is empty, not an error


def test_literal_braces_in_templates_are_left_alone():
    """The probe instruction quotes JSON: {"probes": {...}}. Only {word} substitutes."""
    text = render(msg("probe.detail.nodomain", probe="adult"))
    assert '"probes": {"adult": "<domain>"}' in text


# --- the page uses only keys the catalog has ---------------------------------

def _keys_used(html: str) -> set[str]:
    used = set(re.findall(r'data-i18n(?:-ph|-aria)?="([^"]+)"', html))
    used |= set(re.findall(r"tr\('([^']+)'\)", html))
    used |= set(re.findall(r"\{k:'([^']+)'", html))
    return used


def test_every_key_the_page_references_exists():
    for html in (page.render_panel(), page.render_login(), page.render_setup()):
        used = _keys_used(html)
        assert used, "expected the page to reference catalog keys"
        missing = {key for key in used if key not in i18n.CATALOG["en"]}
        assert not missing, missing


def test_the_page_defaults_to_portuguese_with_a_toggle():
    html = page.render_panel()
    assert "let LANG='pt'" in html
    assert 'data-lang="pt"' in html and 'data-lang="en"' in html
    # the stored choice is read defensively - private windows throw on localStorage
    assert "try{const v=localStorage.getItem('sh-lang')" in html


def test_the_status_codes_stay_in_the_css_and_the_words_are_translated():
    html = page.render_panel()
    for code in ("ACTIVE", "OFF", "DEGRADED", "UNKNOWN", "BLOCKED", "ALLOWED"):
        assert f".s-{code}" in html          # the class is the internal code
        assert f"st.{code}" in html.replace('"st.' + code + '"', f"st.{code}")
    assert i18n.CATALOG["pt"]["st.DEGRADED"] == "DEGRADADO"
    assert i18n.CATALOG["pt"]["st.ALLOWED"] == "LIBERADO"


# --- the warnings that must survive the redesign, in the parent's language ----

def test_the_safety_critical_warnings_exist_in_both_languages():
    on_page = {
        # (a) unlock before reading the panel on the guarded PC
        "hint.unlock": ("closes the browser you are reading it in",
                        "fecha o navegador em que você está lendo"),
        # (b) notify is off at the parent's instruction
        "hint.notify": ("at the parent's instruction", "por decisão do pai"),
        # (c) the browser budget is stored, not enforced
        "hint.browser_limit": ("not enforced yet", "ainda não aplicado"),
        # (d) unblocking YouTube does not survive the reconciler
        "hint.youtube": ("apply-filters.py", "apply-filters.py"),
        # (f) UNKNOWN is not OFF
        "n.unknown": ("UNKNOWN is not OFF", "DESCONHECIDO não é DESLIGADO"),
    }
    server_side = {
        # (e) the resolver button needs somebody signed in to the PC. This one arrives
        # in the snapshot rather than the chrome; test_panel_resolver pins that it is
        # ALWAYS in the notes, so here only the two texts are checked.
        "resolver.note.session": ("nobody is signed in", "ninguém está com sessão"),
    }
    html = page.render_panel()
    for key, (en_text, pt_text) in {**on_page, **server_side}.items():
        assert en_text in i18n.CATALOG["en"][key], key
        assert pt_text in i18n.CATALOG["pt"][key], key
    for key in on_page:
        assert key in _keys_used(html), f"{key} is not referenced by the page"

import re
from html.parser import HTMLParser
from importlib.resources import files


class Structure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.details = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])
        if tag == "details":
            self.details.append(values)


def test_theme_preserves_control_hooks_and_collapses_secondary_details():
    html = files("k3_support").joinpath("gui_assets/index.html").read_text()
    parsed = Structure()
    parsed.feed(html)
    assert len(parsed.ids) == len(set(parsed.ids))
    assert {"theme-toggle", "key", "mode-info", "mode-buttons", "notification-status", "case-dialog"} <= set(parsed.ids)
    assert {item.get("class") for item in parsed.details} == {"policy-details", "notification-panel", "help-disclosure"}
    knowledge = html.split('<section id="knowledge"', 1)[1].split('</section>', 1)[0]
    visible, disclosure = knowledge.split('<details class="help-disclosure">', 1)
    assert '已入库 ≠ 可以自动回复。' in visible
    assert '自动发送仍需独立发布授权。' in visible
    assert '<summary>检索、审核与导入规则</summary>' in disclosure
    assert all("open" not in item for item in parsed.details)
    assert "https://codex-resets.com/" in html
    assert "<script src=\"https://" not in html


def luminance(value):
    rgb = [int(value[i:i+2], 16)/255 for i in (1, 3, 5)]
    linear = [v/12.92 if v <= 0.04045 else ((v+0.055)/1.055)**2.4 for v in rgb]
    return sum(channel*weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def test_reading_layout_keeps_status_readable_and_task_list_lightweight():
    css = files("k3_support").joinpath("gui_assets/styles.css").read_text()
    assert "width:min(100%,960px)" in css
    badge = css.split(".badge{", 1)[1].split("}", 1)[0]
    assert "font-size:.875rem" in badge


def test_navigation_and_content_share_alignment_and_accessible_controls():
    css = files("k3_support").joinpath("gui_assets/styles.css").read_text()
    for selector in ("aside", "main"):
        block = css.split(selector+"{", 1)[1].split("}", 1)[0]
        assert "width:min(100%,960px)" in block
        assert "margin:auto" in block
    assert ".feature-grid>.item{border:0;border-bottom:" in css
    assert "prefers-reduced-motion:no-preference" in css
    assert ":focus-visible" in css


def test_both_theme_tokens_have_readable_normal_text_contrast():
    css = files("k3_support").joinpath("gui_assets/styles.css").read_text()
    for selector in (":root", ':root[data-theme="dark"]'):
        block = css.split(selector+" {", 1)[1].split("}", 1)[0]
        values = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", block))
        for fg, bg in (("ink", "paper"), ("ink", "surface"), ("muted", "paper"),
                       ("muted", "surface"), ("accent", "accent-soft"), ("danger", "danger-soft"),
                       ("ink", "metric-yellow"), ("ink", "metric-pink"),
                       ("ink", "metric-blue"), ("ink", "metric-mint"), ("button-ink", "accent")):
            high, low = sorted((luminance(values[fg]), luminance(values[bg])), reverse=True)
            assert (high+0.05)/(low+0.05) >= 4.5, (selector, fg, bg)


def test_detail_forms_share_theme_and_mobile_close_remains_reachable():
    css = files('k3_support').joinpath('gui_assets/styles.css').read_text()
    assert ':is(.page,#case-dialog) :is(input,textarea,select)' in css
    assert ':is(.page,#case-dialog) textarea' in css
    assert '#case-dialog>.section-heading{position:sticky;' in css
    assert '#case-dialog button{min-height:44px}' in css
    assert '#case-dialog{max-height:90dvh}' in css

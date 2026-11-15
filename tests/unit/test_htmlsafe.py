"""htmlsafe 的四类输出上下文转义契约（P1-04）：文本、属性、URL、JavaScript。

四类上下文各有专门规则、不可互相替代；本文件钉住每一类的负向行为——
恶意输入不得突破上下文，合法内容不得被错误改写。
"""

import json
from html.parser import HTMLParser

from mobile_perf.htmlsafe import html_escape, json_for_script


class _AttrParser(HTMLParser):
    """收集解析到的标签与属性（用于属性/URL 上下文断言）。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.tags: list[str] = []
        self.attrs: list[list[tuple[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.append(list(attrs))

    def handle_endtag(self, tag):
        self.tags.append(f"/{tag}")


_PAYLOAD = "</script><script>alert(1)</script>"


def test_html_text_context_neutralizes_tags():
    """文本上下文：标签字符被实体化，解析器看不到任何注入标签。"""
    out = html_escape(_PAYLOAD)

    assert "<" not in out and ">" not in out
    parser = _AttrParser()
    parser.feed(f"<p>{out}</p>")
    assert parser.tags == ["p", "/p"]  # 只有宿主写死的标签被解析


def test_html_attribute_context_quotes_are_escaped():
    """属性上下文：双引号被实体化，注入不得拆出一个新属性或新标签。"""
    payload = '"><script>alert(1)</script>'
    out = html_escape(payload)

    assert '"' not in out
    parser = _AttrParser()
    parser.feed(f'<a title="{out}">x</a>')
    assert parser.tags == ["a", "/a"]  # 无 script 标签被解析
    assert parser.attrs[0] == [("title", payload)]  # 属性值逐字符还原


def test_url_context_rejects_non_local_paths_in_artifact_links(tmp_path):
    """URL 上下文（产物链接）：存在的本地路径才发 <a href>，伪路径按文本。"""
    from mobile_perf import report as rpt

    real = tmp_path / "artifacts" / "performance.csv"
    real.parent.mkdir(parents=True)
    real.write_text("x", encoding="utf-8")
    built = rpt.build_report(
        meta=_meta_htmlsafe(), record={}, artifacts=[str(real), "javascript:alert(1)"]
    )

    html = rpt._artifact_links_html(built, tmp_path)

    assert f'href="{html_escape(str(real))}"' in html or 'href="' in html
    assert 'href="javascript:alert(1)"' not in html  # 伪路径不可点击
    assert "javascript:alert(1)" in html  # 但值仍如实可见
    # 存在的真实文件仍可点击
    rel = real.relative_to(tmp_path).as_posix()
    assert f'<a href="{rel}">' in html


def _meta_htmlsafe():
    return {
        "runId": "run-safe", "target": "tv.danmaku.bili", "items": [], "serial": "s1",
        "hostRunId": None, "startedAt": "2026-11-15T10:00:00",
        "stoppedAt": "2026-11-15T10:00:10", "durationS": 10.0,
    }


def test_script_context_round_trip_and_no_escape_sequence():
    """script 上下文：无原始闭合序列，JSON 解析后与原值逐字节一致。"""
    values = [
        _PAYLOAD,
        '引号"\\反斜杠',
        "<!-- 双横线",
        "emoji 🚀 中文 空格",
        {"page": _PAYLOAD, "nested": [1, 2.5, None, True]},
    ]
    for value in values:
        out = json_for_script(value)

        assert "<" not in out and ">" not in out and "&" not in out
        assert json.loads(out) == value  # \uXXXX 是合法 JSON 转义，解析即还原

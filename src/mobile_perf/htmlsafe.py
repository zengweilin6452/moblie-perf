"""HTML 报告各输出上下文的统一转义规则（P0-01/P1-04）。

报告渲染把动态数据送进四类上下文，各有专门规则，**不可互相替代**：

- HTML 文本 / 属性：:func:`html_escape`（``html.escape``，默认 quote=True
  覆盖属性引号上下文）；
- 内联 ``<script>`` 数据：:func:`json_for_script`（JSON 序列化 + ``\\uXXXX``
  转义）；
- 产物链接的 href（URL 上下文）：在 report.py 内只对真实存在的本地文件发
  ``<a href>``，其余按纯文本呈现——``html_escape`` 只能保证不突破属性引号，
  挡不住把 ``javascript:`` 伪路径渲染成可点击入口。

注意：``html_escape`` 是 HTML 文本上下文的另一套规则，覆盖不了 script 上下文；
反过来 ``json_for_script`` 产物只能进 ``<script>``，不能当 HTML 文本用。
"""

from __future__ import annotations

import json
from html import escape as html_escape
from typing import Any

__all__ = ["html_escape", "json_for_script"]


def json_for_script(value: Any) -> str:
    """HTML 内联 ``<script>`` 上下文的统一 JSON 序列化（P0-01）。

    ``json.dumps`` 不会转义 ``</``：HTML 解析器在 script 上下文遇到
    ``</script`` 就提前闭合标签，页面名等外部输入借此可注入任意标签后续
    内容。这里把 ``<``、``>``、``&`` 及 U+2028/U+2029 转成 JS 字符串转义
    （``\\uXXXX``），``JSON.parse`` 后与原值逐字节一致。所有进入内联
    JavaScript 的动态数据一律走本函数，不得直接 ``json.dumps``。
    """
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )

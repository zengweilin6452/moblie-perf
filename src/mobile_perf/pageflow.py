"""整页加载耗时（ticket 19）：从页面打标到画面进入**稳定**的耗时。

口径（M6 方案 §7，D5）：

    page_load_ms = (首个「连续 ≥ QUIET_GAP_MS 无新帧」窗口的起点) − 页面起点

**为什么不用截图差分**：``screencap`` 实测 0.45 s/次，采样率不足以分辨百毫秒级
的稳定时刻，且每次拉起渲染管线都会干扰被测 App；帧完成时刻是零成本旁路数据，
精度更高。

本模块只做**纯计算**：输入必须已换算到**同一时间轴**（会话相对秒）——页面起点、
帧完成时刻、观测结束时刻。设备时钟域（``CLOCK_MONOTONIC`` 纳秒）到会话时间轴
的换算由 ``frameinfo.measure_clock_offset`` 做（需要一次 adb 实测），不是纯函数
能做的事。

**已知局限（如实保留，不猜）**：

- 无限轮播/持续动画页面永不静默 → ``timeout``（not_tested），不硬给一个数；
- "稳定"是**视觉稳定**，不保证懒加载数据已就绪（图片可能在稳定后才到）；
- 首帧前的空白期计入耗时——这是用户感知等待的真实组成，不该被剪掉；
- 帧时刻与会话时间轴的对齐精度受 adb 往返延迟限制（数十毫秒量级），
  故本指标用于**跨档位/跨版本的相对比较**，不宜当作毫秒级绝对值；
- 帧完成时刻是 ``CLOCK_MONOTONIC``，设备 ``/proc/uptime`` 是 ``CLOCK_BOOTTIME``，
  二者差累计休眠时间。交互式会话屏幕常亮、无休眠，故以 uptime 换算；若会话中
  设备休眠过，本指标会整体偏移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 稳定判据：相邻帧（或末帧到观测结束）之间至少这么长的静默，才算画面稳定
QUIET_GAP_MS = 300.0

# 超时保护：页面持续动画超过这个时长仍无静默窗口 → not_tested
PAGE_TIMEOUT_MS = 15_000.0

# 入库 canonical key（metrics.METRICS 中登记）
METRIC_KEY = "page_load_ms"

STATUS_OK = "ok"
STATUS_TIMEOUT = "timeout"
STATUS_NO_FRAMES = "no_frames"
STATUS_INCOMPLETE = "incomplete"

_MS_PER_S = 1000.0


@dataclass(frozen=True)
class PageLoad:
    """单个页面的整页加载结论（三态：ok / 超时 / 不可判定）。"""

    page: str
    t0_s: float
    status: str
    page_load_ms: float | None = None
    stable_at_s: float | None = None
    frame_count: int = 0
    last_frame_s: float | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "status": self.status,
            "t0S": round(self.t0_s, 3),
            "pageLoadMs": None if self.page_load_ms is None else round(self.page_load_ms, 3),
            "stableAtS": None if self.stable_at_s is None else round(self.stable_at_s, 3),
            "frameCount": self.frame_count,
            "lastFrameS": None if self.last_frame_s is None else round(self.last_frame_s, 3),
            "reason": self.reason,
        }


def page_load(
    page: str,
    t0_s: float,
    frame_times_s: list[float],
    *,
    observed_until_s: float,
    quiet_gap_ms: float = QUIET_GAP_MS,
    timeout_ms: float = PAGE_TIMEOUT_MS,
) -> PageLoad:
    """判定一个页面的整页加载耗时（纯函数）。

    ``frame_times_s`` 为**帧完成时刻**（会话相对秒，可乱序、可含起点之前的帧）；
    ``observed_until_s`` 为观测结束时刻（通常是逐帧轮询停止时的会话相对秒）——
    没有它就无法区分"页面还在动"与"观测窗口太短、来不及看到静默"。

    四种结论：

    - ``ok``：找到静默窗口，给出 ``page_load_ms``；
    - ``timeout``：观测超过 ``timeout_ms`` 仍无静默窗口（无限动画），或静默点
      晚于超时上限；
    - ``no_frames``：页面起点之后一帧都没有（未渲染/不在前台），不能声称"瞬间
      加载完成"；
    - ``incomplete``：观测窗口太短，既没看到静默也没到超时，无法判定。
    """
    if quiet_gap_ms <= 0:
        raise ValueError("quiet_gap_ms 必须为正数")
    if timeout_ms <= 0:
        raise ValueError("timeout_ms 必须为正数")
    if observed_until_s < t0_s:
        raise ValueError("observed_until_s 不能早于页面起点 t0_s")

    frames = sorted(float(t) for t in frame_times_s if float(t) >= t0_s)
    last_frame_s = frames[-1] if frames else None
    if not frames:
        return PageLoad(
            page=page,
            t0_s=t0_s,
            status=STATUS_NO_FRAMES,
            frame_count=0,
            reason="页面起点之后未观察到任何帧（App 未渲染或不在前台），无法判定稳定",
        )

    # 末帧之后的"静默"同样算数：观测结束时刻是最后一个候选边界
    edges = [*frames, max(observed_until_s, last_frame_s or observed_until_s)]
    for index in range(len(frames)):
        gap_ms = (edges[index + 1] - edges[index]) * _MS_PER_S
        if gap_ms < quiet_gap_ms:
            continue
        stable_at = edges[index]
        load_ms = (stable_at - t0_s) * _MS_PER_S
        if load_ms > timeout_ms:
            return PageLoad(
                page=page,
                t0_s=t0_s,
                status=STATUS_TIMEOUT,
                stable_at_s=stable_at,
                frame_count=len(frames),
                last_frame_s=last_frame_s,
                reason=(
                    f"画面稳定点 {load_ms:.0f} ms 超过上限 {timeout_ms:.0f} ms，"
                    "按不可判定处理（不裁到上限当结果）"
                ),
            )
        return PageLoad(
            page=page,
            t0_s=t0_s,
            status=STATUS_OK,
            page_load_ms=load_ms,
            stable_at_s=stable_at,
            frame_count=len(frames),
            last_frame_s=last_frame_s,
        )

    elapsed_ms = (edges[-1] - t0_s) * _MS_PER_S
    if elapsed_ms > timeout_ms:
        return PageLoad(
            page=page,
            t0_s=t0_s,
            status=STATUS_TIMEOUT,
            frame_count=len(frames),
            last_frame_s=last_frame_s,
            reason=(
                f"观测 {elapsed_ms:.0f} ms 内画面始终有新帧（无限动画/持续刷新），"
                f"未出现 ≥{quiet_gap_ms:.0f} ms 的静默窗口"
            ),
        )
    return PageLoad(
        page=page,
        t0_s=t0_s,
        status=STATUS_INCOMPLETE,
        frame_count=len(frames),
        last_frame_s=last_frame_s,
        reason=(
            f"观测窗口仅 {elapsed_ms:.0f} ms，既未出现 ≥{quiet_gap_ms:.0f} ms 的静默，"
            f"也未到超时上限 {timeout_ms:.0f} ms，无法判定"
        ),
    )


def aggregate_row(results: list[PageLoad]) -> dict[str, Any] | None:
    """把多个页面的结论聚成一行 ``page_load_ms``；没有可判定页面返回 None。

    **多页面口径**：metrics 表主键是 ``(run_id, metric_key)``，同一 run 的多个
    页面无法各占一行。故一行内放**每个页面一个样本**（``sampleCount`` 即页面数），
    逐页数值写在报告 ``pages`` 与 ``page_load`` 事件里。judge 对 ``page_load_ms``
    设阈值时判的是"本次 run 各页面的中位"，跨页面比较前请先看报告逐页值。
    """
    values = [result.page_load_ms for result in results if result.ok]
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    from . import metrics as metrics_module

    return metrics_module.aggregate_row(METRIC_KEY, usable)


def not_tested_reason(
    results: list[PageLoad], *, frames_enabled: bool, page_count: int | None = None
) -> str:
    """整页耗时不可用时的如实原因（含可执行下一步）。

    ``page_count`` 为宿主实际打标的页面数——必须与 ``results`` 分开传：
    打了标却算不出结论（没开逐帧 / 取不到 uptime 偏移）时 ``results`` 为空，
    但"没打标"和"打了标算不出"是两种完全不同的排查方向。
    多页面时把每页的原因拼起来——只报第一条会把"另一页是无限动画"藏起来。
    """
    count = len(results) if page_count is None else page_count
    if count == 0:
        return "未调用 mark_page，本 run 没有页面起点，整页加载耗时不可得"
    if not frames_enabled:
        return "未开启逐帧采集（--frame-poll），没有帧完成时刻，整页加载耗时不可得"
    if not results:
        return (
            "已打页面标但取不到设备 uptime 偏移，帧时刻无法换算到会话时间轴，"
            "本 run 不硬算（检查 /proc/uptime 是否可读）"
        )
    details = "; ".join(
        f"{result.page}（{result.status}: {result.reason}）" for result in results
    )
    return f"没有页面取得可判定的稳定点：{details}"

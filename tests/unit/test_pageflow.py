"""整页加载耗时（ticket 19）：稳定判定纯函数与多页面聚合。

口径：从页面起点 t0 起，找首个「相邻帧间隔 ≥ QUIET_GAP_MS」的位置 t1，
page_load_ms = t1 − t0。五个必测分支见工单验收项。
"""

import pytest

from mobile_perf import pageflow


def test_fast_load_is_short_and_stable():
    """快速加载：几帧之后立刻静默，耗时落在首帧到静默点之间。"""
    result = pageflow.page_load(
        "home",
        0.0,
        [0.10, 0.20, 0.30, 0.40],
        observed_until_s=5.0,
    )

    assert result.status == pageflow.STATUS_OK
    assert result.page_load_ms == pytest.approx(400.0)  # 末帧后 4.6s 静默 → 稳定在 0.4s
    assert result.stable_at_s == pytest.approx(0.40)
    assert result.frame_count == 4
    assert result.reason is None


def test_slow_load_takes_until_last_frame():
    """慢速加载：帧持续到来（间隔小于静默门槛），稳定点落在**最后一帧**。"""
    result = pageflow.page_load(
        "detail",
        0.0,
        [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0],
        observed_until_s=8.0,
    )

    assert result.status == pageflow.STATUS_OK
    assert result.page_load_ms == pytest.approx(2000.0)
    assert result.last_frame_s == pytest.approx(2.0)


def test_mid_load_stall_is_taken_as_stable_point():
    """已知局限的固化：加载中途出现 ≥300 ms 的断帧，会被当成稳定点。

    这是"首个静默窗口"口径的固有代价（网络卡顿时画面确实静止），故本指标
    用于**跨档位/跨版本的相对比较**；要识别"停顿后继续加载"需要更强的判据，
    不在本工单范围。这条用例存在的意义是把该行为钉住，避免日后被当成 bug 修掉。
    """
    result = pageflow.page_load(
        "detail", 0.0, [0.2, 0.6, 1.1], observed_until_s=8.0
    )

    assert result.page_load_ms == pytest.approx(200.0)  # 0.2→0.6 的 400ms 停顿
    assert result.last_frame_s == pytest.approx(1.1)


def test_infinite_animation_times_out_without_a_number():
    """无限动画：帧一直来，观测超时后不给数值，只给不可判定原因。"""
    frames = [index * 0.1 for index in range(200)]  # 20 s 连续 100ms 间隔

    result = pageflow.page_load("carousel", 0.0, frames, observed_until_s=20.0)

    assert result.status == pageflow.STATUS_TIMEOUT
    assert result.page_load_ms is None  # 绝不拿超时上限冒充结果
    assert "无限动画" in result.reason
    assert result.frame_count == 200


def test_zero_frames_is_not_instant_load():
    """零帧：不能声称"瞬间加载完成"，要如实说没观察到帧。"""
    result = pageflow.page_load("blank", 0.0, [], observed_until_s=5.0)

    assert result.status == pageflow.STATUS_NO_FRAMES
    assert result.page_load_ms is None
    assert result.frame_count == 0
    assert "未观察到任何帧" in result.reason


def test_first_frame_then_silence_uses_first_frame_as_stable_point():
    """首帧即静默：稳定点就是首帧本身，耗时 = 首帧时刻 − 页面起点。"""
    result = pageflow.page_load("cached", 1.0, [1.05], observed_until_s=6.0)

    assert result.status == pageflow.STATUS_OK
    assert result.page_load_ms == pytest.approx(50.0)
    assert result.stable_at_s == pytest.approx(1.05)


def test_short_observation_without_silence_is_incomplete_not_timeout():
    """观测窗口太短又没静默：既不是稳定也不是超时，如实说不完整。"""
    result = pageflow.page_load(
        "busy", 0.0, [0.1, 0.2, 0.3], observed_until_s=0.5
    )

    assert result.status == pageflow.STATUS_INCOMPLETE
    assert result.page_load_ms is None
    assert "无法判定" in result.reason


def test_frames_before_page_mark_are_ignored():
    """页面起点之前的帧不属于这个页面：混进来会把耗时算成负数或 0。"""
    result = pageflow.page_load(
        "home", 2.0, [0.5, 1.0, 2.3, 2.6], observed_until_s=8.0
    )

    assert result.frame_count == 2  # 只剩 2.3 与 2.6
    assert result.page_load_ms == pytest.approx(300.0)  # 2.3 → 2.6 之间不静默，稳定在 2.6


def test_unordered_frames_are_sorted():
    """帧时刻可能乱序（跨轮去重后追加），判定前必须排序。"""
    result = pageflow.page_load(
        "home", 0.0, [0.9, 0.1, 0.5], observed_until_s=6.0
    )

    # 排序后 0.1/0.5/0.9，相邻间隔都是 400 ms ≥ 门槛 → 首个静默窗口起点是 0.1
    assert result.page_load_ms == pytest.approx(100.0)
    assert result.last_frame_s == pytest.approx(0.9)


def test_stable_point_beyond_timeout_is_not_clamped():
    """稳定点晚于超时上限：判超时，不把数值裁到上限当结果。

    构造：前 16 s 帧一直不断（间隔 100 ms），之后才静默——静默点已越过 15 s 上限。
    """
    frames = [index * 0.1 for index in range(160)]

    result = pageflow.page_load("slow", 0.0, frames, observed_until_s=30.0)

    assert result.status == pageflow.STATUS_TIMEOUT
    assert result.page_load_ms is None
    assert "超过上限" in result.reason


def test_quiet_gap_is_configurable():
    """静默判据可配：把门槛提到 800 ms 后，400 ms 的空隙就不再算稳定。"""
    frames = [0.1, 0.5, 0.9]

    loose = pageflow.page_load("p", 0.0, frames, observed_until_s=5.0, quiet_gap_ms=300.0)
    strict = pageflow.page_load("p", 0.0, frames, observed_until_s=5.0, quiet_gap_ms=800.0)

    assert loose.status == pageflow.STATUS_OK
    assert strict.status == pageflow.STATUS_OK
    assert strict.page_load_ms > loose.page_load_ms  # 更严的门槛把稳定点推后


def test_invalid_arguments_fail_fast():
    with pytest.raises(ValueError, match="quiet_gap_ms"):
        pageflow.page_load("p", 0.0, [], observed_until_s=1.0, quiet_gap_ms=0)
    with pytest.raises(ValueError, match="timeout_ms"):
        pageflow.page_load("p", 0.0, [], observed_until_s=1.0, timeout_ms=-1)
    with pytest.raises(ValueError, match="observed_until_s"):
        pageflow.page_load("p", 5.0, [], observed_until_s=1.0)


def test_aggregate_row_pools_pages_as_samples():
    """多页面聚合：每页一个样本，sampleCount 即页面数（表主键容不下多行）。"""
    results = [
        pageflow.page_load("home", 0.0, [0.3], observed_until_s=5.0),
        pageflow.page_load("detail", 0.0, [0.9], observed_until_s=5.0),
        pageflow.page_load("carousel", 0.0, [], observed_until_s=5.0),  # 无帧，不参与
    ]

    row = pageflow.aggregate_row(results)

    assert row["metric_key"] == pageflow.METRIC_KEY
    assert row["sample_count"] == 2
    assert row["min"] == pytest.approx(300.0)
    assert row["max"] == pytest.approx(900.0)


def test_aggregate_row_is_none_when_no_page_is_usable():
    results = [pageflow.page_load("carousel", 0.0, [], observed_until_s=5.0)]

    assert pageflow.aggregate_row(results) is None


def test_not_tested_reason_names_every_failing_page():
    """多页面时不能只报第一条：另一页是无限动画这件事必须写出来。"""
    results = [
        pageflow.page_load("carousel", 0.0, [], observed_until_s=5.0),
        pageflow.page_load("busy", 0.0, [0.1, 0.2], observed_until_s=0.3),
    ]

    reason = pageflow.not_tested_reason(results, frames_enabled=True)

    assert "carousel" in reason and "busy" in reason


def test_not_tested_reason_distinguishes_missing_mark_and_missing_frame_poll():
    assert "mark_page" in pageflow.not_tested_reason([], frames_enabled=True)
    assert "--frame-poll" in pageflow.not_tested_reason(
        [pageflow.page_load("home", 0.0, [], observed_until_s=1.0)],
        frames_enabled=False,
    )

"""CollectSession 生命周期（启动 → 打标 → 停止）的单元测试，时钟注入不真实等待。"""

import pytest

from mobile_perf.session import CollectSession


class FakeBackend:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = []

    def start(self):
        self.start_calls += 1
        return "sid-2026"

    def stop(self, session_id, output_dir):
        self.stop_calls.append((session_id, output_dir))
        return {
            "success": True,
            "state": "stopped",
            "sessionId": session_id,
            "artifacts": {
                "performance": {"localDir": str(output_dir), "files": ["performance.csv"]}
            },
        }


def make_session(backend, clock_values, now_values):
    clocks = iter(clock_values)
    nows = iter(now_values)
    return CollectSession(
        target="tv.danmaku.bili",
        items=["CPU", "Memory"],
        serial="749622c9",
        start=backend.start,
        stop=backend.stop,
        clock=lambda: next(clocks),
        now=lambda: next(nows),
    )


def test_start_returns_session_id_and_records_wall_time():
    backend = FakeBackend()
    session = make_session(backend, [100.0, 105.0], ["2026-10-25T10:00:00", "2026-10-25T10:00:05"])

    session_id = session.start()

    assert session_id == "sid-2026"
    assert session.started_at == "2026-10-25T10:00:00"
    assert backend.start_calls == 1


def test_mark_records_elapsed_time_on_session_timeline():
    backend = FakeBackend()
    session = make_session(backend, [0.0, 3.5, 7.25, 10.0], ["t0", "t1", "t2", "t3"])
    session.start()

    session.mark("scroll_home_done")
    session.mark("scroll_end")

    assert [(m.label, m.elapsed_s) for m in session.marks] == [
        ("scroll_home_done", 3.5),
        ("scroll_end", 7.25),
    ]


def test_stop_measures_duration_and_returns_record():
    backend = FakeBackend()
    session = make_session(backend, [0.0, 6.0, 12.5], ["t0", "t1", "t2"])
    session.start()
    session.mark("half_way")

    record = session.stop(output_dir="runs/run-x/artifacts")

    assert record.session_id == "sid-2026"
    assert record.duration_s == 12.5
    assert record.stopped_at == "t1"
    assert record.target == "tv.danmaku.bili"
    assert record.items == ["CPU", "Memory"]
    assert record.serial == "749622c9"
    assert record.stop_payload["state"] == "stopped"
    assert backend.stop_calls == [("sid-2026", "runs/run-x/artifacts")]


def test_mark_before_start_raises():
    backend = FakeBackend()
    session = make_session(backend, [0.0], ["t0"])

    with pytest.raises(RuntimeError, match="start"):
        session.mark("too_early")


def test_double_start_raises():
    backend = FakeBackend()
    session = make_session(backend, [0.0, 1.0, 2.0, 3.0], ["t0", "t1", "t2", "t3"])
    session.start()

    with pytest.raises(RuntimeError, match="already"):
        session.start()


def test_stop_without_start_raises():
    backend = FakeBackend()
    session = make_session(backend, [0.0], ["t0"])

    with pytest.raises(RuntimeError, match="start"):
        session.stop(output_dir="x")

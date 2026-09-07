"""SSE 录制与回放的往返测试：录下来的东西必须能原样回放"""
import pytest

from runner.agent_runner import AgentRunner
from runner.replay import ReplayError, ReplayRunner

SSE_LINES = [
    "event: RUN_STARTED",
    'data: {"type":"RUN_STARTED","run_id":"r-1","thread_id":"t-1","current_message_id":42}',
    "",
    "event: TEXT_MESSAGE_CONTENT",
    'data: {"type":"TEXT_MESSAGE_CONTENT","message_id":"m1","delta":"联系人陈鑫"}',
    "",
    "event: RUN_FINISHED",
    'data: {"type":"RUN_FINISHED","run_id":"r-1","message_cost_credits":3.5}',
]


def _runner(record_dir) -> AgentRunner:
    """绕过 __init__ 的登录，只装配录制所需字段"""
    runner = AgentRunner.__new__(AgentRunner)
    from runner.sse_parser import SSEParser

    runner.parser = SSEParser()
    runner.record_dir = str(record_dir)
    runner.record_tag = ""
    runner.current_case_id = ""
    runner._turn_index = 0
    return runner


class TestTee:
    def test_yields_every_line_unchanged(self, tmp_path):
        runner = _runner(tmp_path)
        sink = []
        out = list(runner._tee(iter(SSE_LINES), sink))
        assert out == SSE_LINES
        assert sink == SSE_LINES

    def test_decodes_bytes_into_sink_but_yields_original(self, tmp_path):
        runner = _runner(tmp_path)
        sink = []
        raw = [b'data: {"type":"RUN_FINISHED"}']
        out = list(runner._tee(iter(raw), sink))
        assert out == raw
        assert sink == ['data: {"type":"RUN_FINISHED"}']

    def test_lazy_not_eager(self, tmp_path):
        """必须边产出边收集，否则会先把整个流读完，失去流式语义"""
        runner = _runner(tmp_path)
        sink = []
        gen = runner._tee(iter(SSE_LINES), sink)
        next(gen)
        assert len(sink) == 1


class TestWriteRecording:
    def test_writes_turn_numbered_file(self, tmp_path):
        runner = _runner(tmp_path)
        runner.set_case("CASE_A")
        runner._turn_index = 1
        runner._write_recording(SSE_LINES)

        path = tmp_path / "CASE_A" / "T01.sse"
        assert path.exists()
        assert path.read_text(encoding="utf-8").splitlines() == SSE_LINES

    def test_multiple_turns_get_separate_files(self, tmp_path):
        runner = _runner(tmp_path)
        runner.set_case("CASE_B")
        for turn in (1, 2, 3):
            runner._turn_index = turn
            runner._write_recording(SSE_LINES)

        names = sorted(p.name for p in (tmp_path / "CASE_B").iterdir())
        assert names == ["T01.sse", "T02.sse", "T03.sse"]

    def test_set_case_resets_turn_index(self, tmp_path):
        runner = _runner(tmp_path)
        runner._turn_index = 7
        runner.set_case("CASE_C")
        assert runner._turn_index == 0

    def test_unknown_case_id_still_recorded(self, tmp_path):
        """没调 set_case 也不能丢数据"""
        runner = _runner(tmp_path)
        runner._turn_index = 1
        runner._write_recording(SSE_LINES)
        assert (tmp_path / "UNKNOWN_CASE" / "T01.sse").exists()

    def test_record_tag_creates_separate_run_directory(self, tmp_path):
        runner = _runner(tmp_path)
        runner.record_tag = "prompt v2/run 1"
        runner.set_case("CASE_TAGGED")
        runner._turn_index = 1

        runner._write_recording(SSE_LINES)

        assert (
            tmp_path / "CASE_TAGGED" / "prompt_v2_run_1" / "T01.sse"
        ).exists()


class TestRoundTrip:
    def test_recorded_stream_replays_to_same_trace(self, tmp_path):
        """录制 → 回放，解析结果必须一致，否则 fixture 不可信"""
        runner = _runner(tmp_path)
        runner.set_case("CASE_RT")
        sink = []
        original = runner.parser.parse(runner._tee(iter(SSE_LINES), sink))
        runner._turn_index = 1
        runner._write_recording(sink)

        replayer = ReplayRunner(str(tmp_path))
        replayer.set_case("CASE_RT")
        replayed = replayer.run_single("任意输入")

        assert replayed.run_id == original.run_id == "r-1"
        assert replayed.reply_text == original.reply_text == "联系人陈鑫"
        assert replayed.cost_credits == original.cost_credits == 3.5
        assert replayed.finished is original.finished is True

    def test_multi_turn_round_trip_order(self, tmp_path):
        runner = _runner(tmp_path)
        runner.set_case("CASE_MT")
        for turn in (1, 2):
            runner._turn_index = turn
            lines = [ln.replace('"delta":"联系人陈鑫"', f'"delta":"第{turn}轮"') for ln in SSE_LINES]
            runner._write_recording(lines)

        replayer = ReplayRunner(str(tmp_path))
        replayer.set_case("CASE_MT")
        traces = replayer.run_multi([{"input": "a"}, {"input": "b"}])

        assert [t.reply_text for t in traces] == ["第1轮", "第2轮"]

    def test_missing_recording_raises(self, tmp_path):
        (tmp_path / "OTHER_CASE").mkdir()
        replayer = ReplayRunner(str(tmp_path))
        replayer.set_case("NOT_RECORDED")
        with pytest.raises(ReplayError) as exc:
            replayer.run_single("x")
        assert "OTHER_CASE" in str(exc.value)  # 报错里列出可用录制

    def test_fewer_recordings_than_turns_raises(self, tmp_path):
        runner = _runner(tmp_path)
        runner.set_case("CASE_SHORT")
        runner._turn_index = 1
        runner._write_recording(SSE_LINES)

        replayer = ReplayRunner(str(tmp_path))
        replayer.set_case("CASE_SHORT")
        with pytest.raises(ReplayError):
            replayer.run_multi([{"input": "a"}, {"input": "b"}])

    def test_replay_dir_must_exist(self):
        with pytest.raises(ReplayError):
            ReplayRunner("no_such_dir_here")

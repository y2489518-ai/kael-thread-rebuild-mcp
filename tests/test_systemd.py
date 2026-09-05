"""systemd 激活补丁的冒烟测试。绝不碰真 unit：全部用不存在的 unit 名。"""
from __future__ import annotations

from pathlib import Path

import pytest

from kael_thread_rebuild.config import RebuildConfig
from kael_thread_rebuild.systemd import SystemdController, make_controller
from kael_thread_rebuild.tmux import TmuxController

FAKE_UNIT = "ktr-DO-NOT-CREATE-nonexistent.service"


def _config(tmp_path: Path, **extra) -> RebuildConfig:
    project = tmp_path / "-root"
    project.mkdir(exist_ok=True)
    raw = {
        "project_dir": str(project),
        "state_dir": str(tmp_path / "state"),
        "claude_workdir": "/root",
        "tmux_target": "DO-NOT-CREATE:9.9",
        "resume_command": ["claude", "--resume", "{session_id}"],
        "activation": "systemd",
        "systemd_unit": FAKE_UNIT,
        "resume_pointer_path": str(tmp_path / "etc" / "resume_session"),
    }
    raw.update(extra)
    return RebuildConfig.from_mapping(raw)


def test_default_stays_tmux(tmp_path):
    config = _config(tmp_path, activation="tmux")
    assert isinstance(make_controller(config), TmuxController)


def test_systemd_selected(tmp_path):
    config = _config(tmp_path)
    assert isinstance(make_controller(config), SystemdController)


def test_bad_activation_rejected(tmp_path):
    with pytest.raises(ValueError):
        _config(tmp_path, activation="docker")


def test_bad_unit_rejected(tmp_path):
    with pytest.raises(ValueError):
        _config(tmp_path, systemd_unit="zhuo-cc")


def test_nonexistent_unit_is_dead_not_exception(tmp_path):
    ctl = SystemdController(_config(tmp_path))
    assert ctl.target_alive() is False
    assert ctl.pane_pid() == ""
    assert ctl.pane_command() == ""


def test_respawn_invalid_session_id_fails_before_touching_anything(tmp_path):
    config = _config(tmp_path)
    ctl = SystemdController(config)
    result = ctl.respawn("not-a-uuid;rm -rf /")
    assert result.ok is False
    assert not Path(config.resume_pointer_path).exists()


def test_respawn_nonexistent_unit_writes_pointer_then_fails_cleanly(tmp_path):
    config = _config(tmp_path, systemd_restart_timeout_seconds=10)
    ctl = SystemdController(config)
    sid = "12345678-1234-1234-1234-123456789abc"
    result = ctl.respawn(sid)
    assert result.ok is False  # unit 不存在，restart 必失败，但不能抛
    assert Path(config.resume_pointer_path).read_text().strip() == sid


def test_poison_pattern_configurable(tmp_path):
    config = _config(tmp_path, poison_pattern=r"(refusal loop|上下文中毒)")
    assert config.poison_pattern.startswith("(refusal loop")
    with pytest.raises(ValueError):
        _config(tmp_path, poison_pattern="(unclosed")


def test_narrow_pattern_ignores_casual_zhongdu():
    from kael_thread_rebuild.transcript import build_source
    rows = []
    for i in range(6):
        rows.append({"type": "user", "uuid": f"u{i}", "parentUuid": None, "timestamp": "2026-09-05T12:00:00Z",
                     "message": {"role": "user", "content": "我今天食物中毒了，毒上下文这个词好好笑"}})
        rows.append({"type": "assistant", "uuid": f"a{i}", "parentUuid": f"u{i}", "timestamp": "2026-09-05T12:00:01Z",
                     "message": {"role": "assistant", "content": [{"type": "text", "text": "poison_score 无毒上下文，中毒探测器自指了"}]}})
    wide = build_source(rows).poison_score
    narrow = build_source(rows, poison_pattern=r"(AUP|Acceptable Use|policy violation|policy blocked|refusal loop|上下文中毒|拒绝循环)").poison_score
    assert wide >= 2, wide
    assert narrow == 0, narrow

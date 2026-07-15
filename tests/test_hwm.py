from contextbot.core.hwm import HighWaterMark


def test_absent_defaults_to_zero(tmp_path):
    hwm = HighWaterMark(tmp_path / "hwm.json")
    assert hwm.exists() is False
    assert hwm.value == 0


def test_advance_persists(tmp_path):
    path = tmp_path / "hwm.json"
    hwm = HighWaterMark(path)
    hwm.advance(10)
    assert hwm.value == 10
    assert path.exists()
    # Reload from disk.
    reloaded = HighWaterMark(path)
    assert reloaded.load() == 10


def test_advance_never_goes_backward(tmp_path):
    hwm = HighWaterMark(tmp_path / "hwm.json")
    hwm.advance(10)
    hwm.advance(5)
    assert hwm.value == 10


def test_baseline_sets_and_persists(tmp_path):
    path = tmp_path / "hwm.json"
    hwm = HighWaterMark(path)
    hwm.baseline(100)
    assert hwm.value == 100
    assert hwm.exists() is True
    assert HighWaterMark(path).load() == 100


def test_corrupt_file_resets_to_zero(tmp_path):
    path = tmp_path / "hwm.json"
    path.write_text("not json", encoding="utf-8")
    hwm = HighWaterMark(path)
    assert hwm.load() == 0

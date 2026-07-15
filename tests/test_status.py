from contextbot.core.status import (
    STATUS_COLORS,
    STATUS_LABELS,
    BotStatus,
    StatusModel,
)


def test_initial_state():
    model = StatusModel()
    assert model.status == BotStatus.STOPPED
    assert model.color == STATUS_COLORS[BotStatus.STOPPED]
    assert model.label == STATUS_LABELS[BotStatus.STOPPED]


def test_set_notifies_observers():
    model = StatusModel()
    seen = []
    model.subscribe(lambda status, msg: seen.append((status, msg)))
    model.set(BotStatus.RUNNING, "실행 중")
    assert seen == [(BotStatus.RUNNING, "실행 중")]
    assert model.status == BotStatus.RUNNING


def test_same_status_still_notifies():
    model = StatusModel()
    seen = []
    model.subscribe(lambda status, msg: seen.append((status, msg)))
    model.set(BotStatus.PROCESSING, "1")
    model.set(BotStatus.PROCESSING, "2")
    assert len(seen) == 2


def test_unsubscribe():
    model = StatusModel()
    seen = []
    obs = lambda status, msg: seen.append(status)  # noqa: E731
    model.subscribe(obs)
    model.unsubscribe(obs)
    model.set(BotStatus.ERROR)
    assert seen == []


def test_all_statuses_have_color_and_label():
    for status in BotStatus:
        assert status in STATUS_COLORS
        assert status in STATUS_LABELS

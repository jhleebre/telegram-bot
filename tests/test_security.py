from contextbot.core.security import is_saved_messages


def test_own_saved_messages():
    assert is_saved_messages(42, 42) is True


def test_other_chat():
    assert is_saved_messages(99, 42) is False


def test_none_peer():
    assert is_saved_messages(None, 42) is False


def test_none_me():
    assert is_saved_messages(42, None) is False

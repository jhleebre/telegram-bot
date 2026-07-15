import pytest

from contextbot.engine.parsing import ParseError, extract_json_object


def test_plain_json():
    assert extract_json_object('{"title": "메모", "tags": ["a"]}') == {
        "title": "메모",
        "tags": ["a"],
    }


def test_surrounding_whitespace():
    assert extract_json_object('\n\n  {"a": 1}  \n') == {"a": 1}


@pytest.mark.parametrize("fence", ["```json", "```JSON", "```"])
def test_fenced_json(fence):
    assert extract_json_object(f'{fence}\n{{"a": 1}}\n```') == {"a": 1}


def test_prose_around_object():
    text = 'Here is the metadata you asked for:\n{"a": 1}\nHope that helps!'
    assert extract_json_object(text) == {"a": 1}


def test_nested_braces_recovered_from_prose():
    text = 'Result: {"a": {"b": [1, 2]}} done'
    assert extract_json_object(text) == {"a": {"b": [1, 2]}}


def test_non_object_json_rejected():
    with pytest.raises(ParseError):
        extract_json_object("[1, 2, 3]")


def test_garbage_rejected():
    with pytest.raises(ParseError):
        extract_json_object("I could not do that.")


def test_empty_rejected():
    with pytest.raises(ParseError):
        extract_json_object("   ")

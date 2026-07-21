import pytest

from mua_bot.adapters.llm import LLMResponseError, extract_json_object


def test_extract_json_from_fence() -> None:
    result = extract_json_object('answer:\n```json\n{"safe": true}\n```')
    assert result == {"safe": True}


def test_extract_json_rejects_non_json() -> None:
    with pytest.raises(LLMResponseError):
        extract_json_object("no structured result")

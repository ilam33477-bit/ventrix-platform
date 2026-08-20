from services.backend.analysis.service import russian_user_text
from services.backend.client_bots.handlers import (
    CONNECTION_STATUS_LABELS,
    PROBLEM_STATUS_LABELS,
    PROBLEM_TYPE_LABELS,
)
from services.backend.intelligence.ai_triage import AITriageService


def test_generated_user_copy_falls_back_when_output_is_english_or_mostly_english() -> None:
    fallback = "Клиент запросил подробности, сотруднику нужно ответить."
    assert russian_user_text("Client asked for more details.", fallback) == fallback
    assert russian_user_text("Client asked for details — клиент ждёт.", fallback) == fallback
    assert AITriageService._russian_text("Employee should respond.", fallback) == fallback
    assert (
        russian_user_text("Клиент запросил подробности по Telegram.", fallback)
        == "Клиент запросил подробности по Telegram."
    )


def test_client_bot_presentation_maps_hide_internal_enum_values() -> None:
    assert PROBLEM_TYPE_LABELS["client_without_answer"] == "Клиент ждёт ответа"
    assert PROBLEM_STATUS_LABELS["needs_confirmation"] == "Нужно проверить"
    assert CONNECTION_STATUS_LABELS["reauthorization_required"] == "нужно подключить заново"
    assert all(key != value for key, value in PROBLEM_TYPE_LABELS.items())
    assert all(key != value for key, value in PROBLEM_STATUS_LABELS.items())
    assert all(key != value for key, value in CONNECTION_STATUS_LABELS.items())

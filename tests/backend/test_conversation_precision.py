from services.backend.intelligence.conversation_state import assess_conversation


def dialog(*items: tuple[str, bool]) -> list[dict[str, object]]:
    return [
        {"id": index, "text": text, "outgoing": outgoing}
        for index, (text, outgoing) in enumerate(items, start=1)
    ]


def test_case_a_polite_acknowledgement_is_closed() -> None:
    result = assess_conversation(
        dialog(
            ("если что-то будет непонятно, поддержка всегда на связи", True),
            ("а, спасибо большое)", False),
        )
    )
    assert result.conversation_state == "CLOSED_SUCCESS"
    assert not result.action_required


def test_case_b_multiclause_acknowledgement_is_closed() -> None:
    result = assess_conversation(
        dialog(
            ("заходи в бота, настройка займёт минутку", True),
            ("Все, хорошо) Поняла, спасибо большое 🙏🏻", False),
        )
    )
    assert result.conversation_state == "CLOSED_SUCCESS"
    assert not result.response_required


def test_case_c_accepted_refusal_remains_closed_after_thanks() -> None:
    result = assess_conversation(
        dialog(
            ("Через ботов не работаю", False),
            ("Понял, без проблем. Если надумаешь — я на связи", True),
            ("Хорошо, спасибо за предложение", False),
        )
    )
    assert result.conversation_state == "CLOSED_REJECTED"
    assert not result.action_required


def test_case_d_testing_acknowledgement_is_closed() -> None:
    result = assess_conversation(
        dialog(
            ("если что-то непонятно, пиши", True),
            ("Хорошо, потестирую", False),
        )
    )
    assert result.conversation_state == "CLOSED_SUCCESS"


def test_case_e_offer_question_requires_employee() -> None:
    result = assess_conversation(dialog(("Какое предложение?", False)))
    assert result.conversation_state == "WAITING_FOR_EMPLOYEE"
    assert result.issue_family == "UNANSWERED_REQUEST"


def test_case_f_payment_failure_requires_employee() -> None:
    result = assess_conversation(dialog(("А сколько стоит подписка на бот?", False)))
    assert result.issue_family == "PAYMENT_QUESTION"
    assert result.action_required


def test_case_g_technical_problem_requires_employee() -> None:
    result = assess_conversation(
        dialog(
            ("бот не открывается", False),
            ("попробуй ещё раз", True),
            ("всё равно просто загрузка", False),
        )
    )
    assert result.conversation_state == "ACTIVE_SUPPORT"
    assert result.issue_family == "TECHNICAL_PROBLEM"


def test_case_h_employee_pitch_waits_for_client() -> None:
    result = assess_conversation(dialog(("Будет интересно попробовать?", True)))
    assert result.conversation_state == "WAITING_FOR_CLIENT"
    assert not result.action_required


def test_case_i_employee_sent_link_waits_for_client() -> None:
    result = assess_conversation(
        dialog(
            ("Интересно", False),
            ("Вот ссылка, попробуй", True),
        )
    )
    assert result.conversation_state == "WAITING_FOR_CLIENT"
    assert not result.response_required


def test_case_j_reengaged_lead_is_one_specific_opportunity() -> None:
    result = assess_conversation(dialog(("Стало интересно, как это работает?", False)))
    assert result.issue_family == "COMMERCIAL_OPPORTUNITY"
    assert result.action_required


def test_case_k_direct_refusal_is_closed() -> None:
    result = assess_conversation(
        dialog(("Спасибо большое за предложение, но система не интересует", False))
    )
    assert result.conversation_state == "CLOSED_REJECTED"


def test_case_l_explicit_request_requires_employee() -> None:
    result = assess_conversation(
        dialog(("мне куда-то жмякать, чтоб искал, или просто ждать?", False))
    )
    assert result.issue_family == "UNANSWERED_REQUEST"
    assert result.response_required

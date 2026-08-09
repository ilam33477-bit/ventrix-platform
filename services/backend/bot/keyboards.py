from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo


def owner_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 Клиенты", callback_data="owner:clients"),
                InlineKeyboardButton(text="➕ Создать", callback_data="owner:tenant_create"),
            ],
            [
                InlineKeyboardButton(text="🤖 Клиентские боты", callback_data="owner:bots"),
                InlineKeyboardButton(text="📊 Активность", callback_data="owner:activity"),
            ],
            [
                InlineKeyboardButton(text="🟢 Состояние", callback_data="owner:system"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="owner:settings"),
            ],
        ]
    )


def back_to_owner_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="owner:menu")]]
    )


def cancel_flow() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✕ Отменить действие", callback_data="flow:cancel")],
            [InlineKeyboardButton(text="↻ Начать заново", callback_data="flow:restart")],
        ]
    )


def tenant_create_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✨ Описать клиента текстом", callback_data="owner:tenant_create:ai"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📝 Заполнить вручную", callback_data="owner:tenant_create:manual"
                )
            ],
            [InlineKeyboardButton(text="← Главное меню", callback_data="owner:menu")],
        ]
    )


def ai_draft_confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✓ Создать клиента", callback_data="flow:ai_draft:confirm")],
            [InlineKeyboardButton(text="☷ Изменить поле", callback_data="flow:ai_draft:fields")],
            [
                InlineKeyboardButton(
                    text="✎ Исправить текстом", callback_data="flow:ai_draft:correct"
                )
            ],
            [InlineKeyboardButton(text="✕ Отменить", callback_data="flow:cancel")],
        ]
    )


def ai_draft_field_selector() -> InlineKeyboardMarkup:
    fields = (
        ("Компания", "name"),
        ("Telegram ID", "owner_telegram_user_id"),
        ("Ниша", "niche"),
        ("Продукты", "products_services"),
        ("Аудитория", "target_audience"),
        ("SLA", "response_sla_minutes"),
        ("Отчёт", "daily_report_time"),
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"flow:ai_draft:field:{field}")]
            for label, field in fields
        ]
        + [[InlineKeyboardButton(text="← К черновику", callback_data="flow:ai_draft:back")]]
    )


def optional_username() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="flow:username:skip")],
            [InlineKeyboardButton(text="✕ Отменить", callback_data="flow:cancel")],
        ]
    )


def optional_access_end() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Без ограничения", callback_data="flow:access:none")],
            [InlineKeyboardButton(text="✕ Отменить", callback_data="flow:cancel")],
        ]
    )


def tenant_bot_missing(tenant_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Подключить бота", callback_data=f"bot:create:{tenant_id}"
                )
            ],
            [InlineKeyboardButton(text="← Карточка", callback_data=f"tenant:view:{tenant_id}")],
        ]
    )


def ai_recommendation_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✓ Типовой вариант", callback_data="flow:ai:default")],
            [InlineKeyboardButton(text="✎ Ввести свои", callback_data="flow:ai:custom")],
            [
                InlineKeyboardButton(
                    text="✨ Сгенерировать по нише", callback_data="flow:ai:generate"
                )
            ],
            [InlineKeyboardButton(text="✕ Отменить", callback_data="flow:cancel")],
        ]
    )


def flow_confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✓ Подтвердить", callback_data="flow:confirm")],
            [InlineKeyboardButton(text="✎ Изменить поле", callback_data="flow:change")],
            [InlineKeyboardButton(text="✕ Отменить", callback_data="flow:cancel")],
        ]
    )


def create_field_selector() -> InlineKeyboardMarkup:
    fields = [
        ("Компания", "name"),
        ("Telegram ID", "owner_telegram_user_id"),
        ("Username", "owner_telegram_username"),
        ("Ниша", "niche"),
        ("Аудитория", "target_audience"),
        ("AI-рекомендации", "additional_ai_instructions"),
        ("Дата доступа", "subscription_expires_at"),
    ]
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"flow:change:{field}")]
        for label, field in fields
    ]
    rows.append([InlineKeyboardButton(text="← К подтверждению", callback_data="flow:review")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tenant_edit_selector(tenant_id: str) -> InlineKeyboardMarkup:
    fields = [
        ("Компания", "name"),
        ("Telegram ID", "owner_telegram_user_id"),
        ("Username", "owner_telegram_username"),
        ("Ниша", "niche"),
        ("Аудитория", "target_audience"),
        ("Дата доступа", "subscription_expires_at"),
    ]
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"tenant:edit_field:{field}:{tenant_id}")]
        for label, field in fields
    ]
    rows.append([InlineKeyboardButton(text="← Карточка", callback_data=f"tenant:view:{tenant_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✓ Сохранить", callback_data="tenant:edit_confirm")],
            [InlineKeyboardButton(text="✕ Отменить", callback_data="flow:cancel")],
        ]
    )


def ai_profile_actions(tenant_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✎ Изменить рекомендации", callback_data=f"ai:edit:{tenant_id}"
                )
            ],
            [InlineKeyboardButton(text="← Карточка", callback_data=f"tenant:view:{tenant_id}")],
        ]
    )


def tenant_actions(tenant_id: str, *, suspended: bool = False) -> InlineKeyboardMarkup:
    access_action = "resume" if suspended else "suspend"
    access_label = "▶ Возобновить" if suspended else "⏸ Приостановить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✎ Изменить", callback_data=f"tenant:edit:{tenant_id}"),
                InlineKeyboardButton(text="🧠 AI-настройки", callback_data=f"ai:view:{tenant_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="🤖 Клиентский бот", callback_data=f"tenant:bot:{tenant_id}"
                ),
                InlineKeyboardButton(
                    text="📊 Активность", callback_data=f"tenant:activity:{tenant_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗓 Продлить доступ", callback_data=f"tenant:access:{tenant_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="▶ Проверить сейчас", callback_data=f"tenant:analysis_now:{tenant_id}"
                ),
                InlineKeyboardButton(
                    text="■ Отменить анализ", callback_data=f"tenant:analysis_cancel:{tenant_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=access_label, callback_data=f"tenant:{access_action}:{tenant_id}"
                )
            ],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"tenant:delete:{tenant_id}")],
            [InlineKeyboardButton(text="← Клиенты", callback_data="owner:clients")],
        ]
    )


def tenant_selector(tenants: list[tuple[str, str]], action: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"{action}:{tenant_id}")]
        for tenant_id, name in tenants
    ]
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="owner:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def access_actions(tenant_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+7 дней", callback_data=f"tenant:extend:7:{tenant_id}"),
                InlineKeyboardButton(
                    text="+30 дней", callback_data=f"tenant:extend:30:{tenant_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Указать дату", callback_data=f"tenant:extend:date:{tenant_id}"
                )
            ],
            [InlineKeyboardButton(text="← Карточка", callback_data=f"tenant:view:{tenant_id}")],
        ]
    )


def delete_confirmation(tenant_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Удалить безвозвратно", callback_data=f"tenant:delete_confirm:{tenant_id}"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=f"tenant:view:{tenant_id}")],
        ]
    )


def bot_actions(bot_id: str, username: str) -> InlineKeyboardMarkup:
    prefix = "bot:action:"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✓ Проверить", callback_data=f"{prefix}check:{bot_id}"),
                InlineKeyboardButton(
                    text="↻ Перезапустить", callback_data=f"{prefix}restart:{bot_id}"
                ),
            ],
            [
                InlineKeyboardButton(text="▶ Запустить", callback_data=f"{prefix}start:{bot_id}"),
                InlineKeyboardButton(text="■ Остановить", callback_data=f"{prefix}stop:{bot_id}"),
            ],
            [InlineKeyboardButton(text="Открыть в Telegram", url=f"https://t.me/{username}")],
            [
                InlineKeyboardButton(
                    text="Сменить token", callback_data=f"{prefix}rotate:{bot_id}"
                ),
                InlineKeyboardButton(text="Статистика", callback_data=f"{prefix}stats:{bot_id}"),
            ],
            [InlineKeyboardButton(text="← Боты", callback_data="owner:bots")],
        ]
    )


def client_main_menu(mini_app_url: str | None = None) -> InlineKeyboardMarkup:
    panel = (
        InlineKeyboardButton(text="↗ Открыть панель", web_app=WebAppInfo(url=mini_app_url))
        if mini_app_url
        else InlineKeyboardButton(text="↗ Открыть панель", callback_data="client:panel")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📌 Сводка", callback_data="client:summary"),
                InlineKeyboardButton(text="⚠️ Важное", callback_data="client:important"),
            ],
            [
                InlineKeyboardButton(text="📄 Отчёты", callback_data="client:reports"),
                InlineKeyboardButton(text="🔗 Подключения", callback_data="client:connections"),
            ],
            [panel, InlineKeyboardButton(text="⚙️ Настройки", callback_data="client:settings")],
            [
                InlineKeyboardButton(text="👥 Сотрудники", callback_data="client:employees"),
                InlineKeyboardButton(text="🏢 Рабочие группы", callback_data="client:groups"),
            ],
        ]
    )


def client_welcome_menu(mini_app_url: str | None = None) -> InlineKeyboardMarkup:
    panel = (
        InlineKeyboardButton(text="↗ Открыть панель", web_app=WebAppInfo(url=mini_app_url))
        if mini_app_url
        else InlineKeyboardButton(text="↗ Открыть панель", callback_data="client:panel")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✓ Настроить проект", callback_data="client:onboarding")],
            [InlineKeyboardButton(text="🔗 Подключить Telegram", callback_data="client:connect")],
            [panel],
            [InlineKeyboardButton(text="Как это работает", callback_data="client:how")],
            [InlineKeyboardButton(text="Перейти в главное меню", callback_data="client:menu")],
        ]
    )


def back_to_client_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="client:menu")]]
    )

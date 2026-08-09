from aiogram.fsm.state import State, StatesGroup


class TenantCreateStates(StatesGroup):
    name = State()
    owner_user_id = State()
    owner_username = State()
    niche = State()
    audience = State()
    ai_choice = State()
    ai_custom = State()
    access_end = State()
    edit_value = State()
    confirm = State()


class TenantAICreateStates(StatesGroup):
    owner_name = State()
    owner_user_id = State()
    company_name = State()
    ai_choice = State()
    prompt = State()
    confirm = State()
    correction = State()


class SystemSecretStates(StatesGroup):
    value = State()
    confirm = State()


class TenantEditStates(StatesGroup):
    value = State()
    confirm = State()


class TenantAccessStates(StatesGroup):
    date = State()


class TenantHistoryStates(StatesGroup):
    value = State()


class AIProfileStates(StatesGroup):
    recommendations = State()


class BotCreateStates(StatesGroup):
    token = State()


class BotRotateStates(StatesGroup):
    token = State()

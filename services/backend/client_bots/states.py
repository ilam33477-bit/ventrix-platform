from aiogram.fsm.state import State, StatesGroup


class TelegramConnectionStates(StatesGroup):
    phone = State()
    code = State()
    password = State()

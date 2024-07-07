from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

async def sold_out(id, count_click=''):
    kb = InlineKeyboardBuilder()
    if count_click == '':
        kb.add(InlineKeyboardButton(text=f"Товар закончился? Нажми на меня!", callback_data=id))
    else:
        kb.add(InlineKeyboardButton(text=f"Товар закончился? Нажми на меня! ({count_click})", callback_data=id))
    return kb.as_markup()
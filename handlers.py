import asyncio

from aiogram import Router, F
from aiogram.types import  CallbackQuery
from aiogram.enums import ParseMode

import kb, db
from main import bot, channel_id

rout = Router()

click_count = 0
@rout.callback_query()
async def sold_out(cb: CallbackQuery):
    global click_count
    user_id = cb.from_user.id
    vk_id = cb.data
    list_users_id = await db.get_list_user_id(vk_id)
    #msg_id = cb.message.message_id 
    text = cb.message.text
    click_count = await db.get_value('click_count', vk_id)
    click_count = click_count[0]

    if user_id in list_users_id:
        await cb.answer("Вы уже проголосовали!")
    else:
        click_count += 1
        list_users_id.append(user_id)
        await db.insert_list_user_id(list_users_id, vk_id)
        await db.insert_value_to_vk_id('click_count', click_count, vk_id)

        if click_count >= 5 or user_id == 637460660:
            await cb.message.edit_text(f"{text}\n\n‼️Товар закончился‼️", parse_mode=ParseMode.HTML)
        else:
            kbn = await kb.sold_out(cb.data, click_count)
            await cb.message.edit_text(f"{text}", reply_markup=kbn)
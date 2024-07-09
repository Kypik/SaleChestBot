import asyncio
import logging

from config_reader import config
from db import creat_table
import handlers, vk, kb
from aiogram import Bot, Dispatcher
from aiogram.types import InputMediaPhoto

bot = Bot(token = config.tg_bot_token.get_secret_value())
channel_id = '@teestmybot'

async def main():
    logging.basicConfig(level=logging.INFO)

    await creat_table()
    
    dp = Dispatcher()
    
    dp.include_router(handlers.rout)

    creat_task = asyncio.create_task(creat_new_post())
    polling_task = asyncio.create_task(dp.start_polling(bot))
    
    await asyncio.gather(creat_task, polling_task)

async def creat_new_post():
    while True:
        post = await vk.get_new_post()
        if post:
            id = post['id']
            photos = post['photos']
            text = post['text']
            kbn = await kb.sold_out(id)
            if len(photos) == 1:
                await bot.send_photo(chat_id=channel_id, photo=photos[0], caption=text, reply_markup=kbn)
                
            else:
                media = [InputMediaPhoto(media=photo) for photo in photos]
                await bot.send_media_group(chat_id=channel_id, media=media)
                await bot.send_message(chat_id=channel_id, text=text, reply_markup=kbn)

        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
        
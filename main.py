import asyncio
import logging
import sys

from config_reader import config
from db import creat_table, insert_vk_id
import handlers, vk, kb, draft
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TelegramAPIServer
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetUpdates
from aiogram.types import InputMediaPhoto

#channel_id = '@salechest'
channel_id = '@teestmybot'

# Консоль Windows (cp1251) не умеет печатать эмодзи — заменяем их на '?', а не падаем
sys.stderr.reconfigure(errors='replace')
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler = logging.FileHandler('.log', encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logging.getLogger().addHandler(file_handler)


class RetryMiddleware(BaseRequestMiddleware):
    """Соединения через Cloudflare иногда рвутся — повторяем запрос до 3 раз."""
    async def __call__(self, make_request, bot, method):
        chat_id = str(getattr(method, 'chat_id', ''))
        # getUpdates повторяет сам polling; посты в канал не повторяем, чтобы не было дублей
        attempts = 1 if isinstance(method, GetUpdates) or chat_id.startswith('@') else 3
        for attempt in range(1, attempts + 1):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError as e:
                if attempt == attempts:
                    raise
                logging.warning(f"{type(method).__name__}: {e} — повтор {attempt}/{attempts - 1}")
                await asyncio.sleep(1)


def create_bot():
    api = TelegramAPIServer.from_base(config.tg_api_url) if config.tg_api_url else PRODUCTION
    # 25 секунд вместо 60: зависшее соединение быстрее обрывается и повторяется
    session = AiohttpSession(api=api, proxy=config.tg_proxy, timeout=25)
    session.middleware(RetryMiddleware())
    if config.tg_proxy:
        logging.info(f"Telegram через прокси {config.tg_proxy}")
    if config.tg_api_url:
        logging.info(f"Telegram через {config.tg_api_url}")
        # Соединения с Cloudflare из РФ подвисают после ~16 КБ трафика,
        # поэтому не переиспользуем их: каждый запрос — новое соединение
        session._connector_init['force_close'] = True
    return Bot(token=config.tg_bot_token.get_secret_value(), session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def main():
    await creat_table()

    bot = create_bot()
    dp = Dispatcher()
    dp.include_router(draft.rout)      # черновики постов (только админы) — раньше handlers,
    dp.include_router(handlers.rout)   # потому что handlers ловит все остальные кнопки

    # Репост из VK пока выключен. Перед включением: get_last_id в db.py ждёт числовые id,
    # а посты из бота пишутся как 'm<время>' — это нужно будет развести
    # post_task = asyncio.create_task(creat_new_post(bot))
    # Обрывы во время работы start_polling переживает сам, но если связи нет
    # уже при запуске (или Telegram ответил мусором), он падает — перезапускаем
    while True:
        try:
            await dp.start_polling(bot)
            break  # остановлен вручную
        except Exception as e:
            logging.error(f"Polling упал: {e}. Перезапуск через 10 секунд")
            await asyncio.sleep(10)


async def send_post(bot, post):
    id = post['id']
    photos = post['photos']
    text = post['text']
    kbn = await kb.sold_out(id)
    if len(photos) == 1:
        await bot.send_photo(chat_id=channel_id, photo=photos[0], caption=text, reply_markup=kbn)
    elif len(photos) > 1:
        media = [InputMediaPhoto(media=photo) for photo in photos]
        await bot.send_media_group(chat_id=channel_id, media=media)
        if text:
            await bot.send_message(chat_id=channel_id, text=text, reply_markup=kbn)
    elif text:
        await bot.send_message(chat_id=channel_id, text=text, reply_markup=kbn)


async def creat_new_post(bot):
    while True:
        post = None
        try:
            post = await vk.get_new_post()
            if post:
                await send_post(bot, post)
                await insert_vk_id(int(post['id']))
                logging.info(f"Пост {post['id']} опубликован")
        except TelegramBadRequest as e:
            # Telegram отклонил сам пост (например, слишком длинная подпись) — повторять бесполезно
            logging.error(f"Telegram отклонил пост {post['id']}, пропускаем: {e}")
            await insert_vk_id(int(post['id']))
        except Exception as e:
            # Нет связи с VK/Telegram — пост не помечен, попробуем ещё раз через 30 секунд
            logging.error(f"Ошибка при публикации поста: {e}")

        await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())

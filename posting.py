import io
import logging
import re

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaPhoto, LinkPreviewOptions
from PIL import Image

from config_reader import config
from parser import http_get, new_session

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
CAPTION_LIMIT = 1024


def build_text(d):
    """Текст поста в формате канала (HTML)."""
    if d.get('full'):
        return d['full']
    lines = []
    if d['shop']:
        lines += [f"{d['shop']}:", '']
    main = d['title'] or '…'
    if d['price']:
        main += f" за {d['price']}руб."
    lines.append(main)
    if d['link']:
        lines.append(d['link'])
    if d['extra']:
        lines += ['', d['extra']]
    return '\n'.join(lines)


async def send_post(bot, chat_id, text, photos, reply_markup=None):
    """Отправляет пост как в канале: фото + текст + кнопка. Возвращает список сообщений."""
    if len(photos) == 1 and len(text) <= CAPTION_LIMIT:
        msg = await bot.send_photo(chat_id, photo=photos[0], caption=text,
                                   parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return [msg]

    messages = []
    if len(photos) == 1:
        messages.append(await bot.send_photo(chat_id, photo=photos[0]))
    elif photos:
        media = [InputMediaPhoto(media=p) for p in photos]
        messages += await bot.send_media_group(chat_id, media=media)
    messages.append(await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                           reply_markup=reply_markup, link_preview_options=NO_PREVIEW))
    return messages


async def download_as_jpeg(url):
    """Скачивает картинку с домашнего IP и перекодирует в JPEG (Telegram не всегда берёт webp по ссылке)."""
    async with new_session() as s:
        resp = await http_get(s, url)
        resp.raise_for_status()
        data = resp.content
    img = Image.open(io.BytesIO(data)).convert('RGB')
    img.thumbnail((1600, 1600))  # поменьше — загрузка идёт через воркер, крупные файлы могут застрять
    out = io.BytesIO()
    img.save(out, 'JPEG', quality=85)
    return BufferedInputFile(out.getvalue(), filename='photo.jpg')


def is_url(p):
    return isinstance(p, str) and p.startswith('http')


def smaller(url):
    """У WB «big»-фото бывают слишком тяжёлыми для Telegram — берём размер каталога."""
    if 'wbbasket.ru' in url and '/images/big/' in url:
        return url.replace('/images/big/', '/images/c516x688/')
    return url


async def send_preview(bot, chat_id, text, photos):
    """
    Отправляет черновик админу. Фото по ссылкам скачивает сам Telegram; если какое-то не
    скачалось — пробуем меньший размер, потом убираем это фото.
    Возвращает (сообщения, file_id фото, ошибка или None) — file_id потом используются для публикации.
    """
    photos, skipped, tried_smaller = list(photos), 0, False
    for _ in range(2 * len(photos) + 3):
        try:
            messages = await send_post(bot, chat_id, text, photos)
            break
        except TelegramBadRequest as e:
            logging.warning(f'Фото не прошли: {e}')
            m = re.search(r'message #(\d+)', str(e))
            i = int(m.group(1)) - 1 if m else -1
            if 0 <= i < len(photos):                     # Telegram назвал конкретное фото
                if is_url(photos[i]) and smaller(photos[i]) != photos[i]:
                    photos[i] = smaller(photos[i])
                else:
                    photos.pop(i)
                    skipped += 1
            elif not tried_smaller and any(is_url(p) and smaller(p) != p for p in photos):
                photos, tried_smaller = [smaller(p) if is_url(p) else p for p in photos], True
            elif any(is_url(p) for p in photos):
                if config.tg_api_url:
                    # Через Cloudflare крупные загрузки рвутся — просто убираем фото по ссылкам
                    skipped += sum(1 for p in photos if is_url(p))
                    photos = [p for p in photos if not is_url(p)]
                else:
                    # Без воркера можно скачать фото и загрузить самим
                    new = []
                    for p in photos:
                        f = await download_or_none(p) if is_url(p) else p
                        if f:
                            new.append(f)
                        else:
                            skipped += 1
                    photos = new
            elif photos:
                skipped += len(photos)
                photos = []
            else:
                raise
    else:
        messages = await send_post(bot, chat_id, text, [])

    error = f'не загрузилось фото: {skipped} шт. — добавь скриншот' if skipped else None
    file_ids = [m.photo[-1].file_id for m in messages if m.photo]
    return messages, file_ids, error


async def download_or_none(url):
    try:
        return await download_as_jpeg(url)
    except Exception as e:
        logging.warning(f'Не скачалось фото {url}: {e}')
        return None

import asyncio
import logging
import re
import time
from html import escape

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db, kb
from config_reader import config
from parser import MAX_PHOTOS, parse
from posting import build_text, send_post, send_preview

channel_id = '@salechest'

rout = Router()
rout.message.filter(F.chat.type == 'private')

ADMINS = {int(x) for x in re.findall(r'\d+', config.admin_ids or '')}
is_admin = F.from_user.id.in_(ADMINS)

URL_RE = re.compile(r'https?://\S+')

# Черновики в памяти: {user_id: {...}}. При перезапуске бота теряются — это нормально
drafts = {}
render_tasks = {}

FIELDS = {
    'shop':  'магазин (например: AliExpress, Wb, Ozon)',
    'title': 'название товара',
    'price': 'цену, только число (например: 249)',
    'link':  'ссылку на товар',
    'extra': 'дополнительный текст — он встанет под ссылкой',
    'full':  'весь текст поста целиком — он заменит шаблон',
}
WARN_NAMES = {'title': 'название', 'price': 'цена', 'photos': 'фото'}


class Edit(StatesGroup):
    waiting = State()


def new_draft(parsed=None):
    parsed = parsed or {}
    return {
        'shop': parsed.get('shop', ''),
        'title': parsed.get('title', ''),
        'price': parsed.get('price', ''),
        'link': escape(parsed.get('link', '')),
        'extra': '',
        'full': '',
        'photos': list(parsed.get('photos', [])),
        'site_photos': list(parsed.get('photos', [])),
        'msg_ids': [],
        'error': None,
        'blocked': parsed.get('blocked', False),
        'from_link': bool(parsed),
    }


def control_kb(d):
    b = InlineKeyboardBuilder()
    b.button(text='✏️ Магазин', callback_data='d:shop')
    b.button(text='✏️ Название', callback_data='d:title')
    b.button(text='✏️ Цена', callback_data='d:price')
    b.button(text='🔗 Ссылка', callback_data='d:link')
    b.button(text='📝 Доп. текст', callback_data='d:extra')
    if d['full']:
        b.button(text='↩️ Вернуть шаблон', callback_data='d:unfull')
    else:
        b.button(text='✍️ Весь текст вручную', callback_data='d:full')
    b.button(text=f"🖼 Фото ({len(d['photos'])})", callback_data='d:photos')
    b.button(text='✅ Опубликовать', callback_data='d:pub')
    b.button(text='❌ Отмена', callback_data='d:cancel')
    b.adjust(3, 2, 1, 1, 2)
    return b.as_markup()


def photos_kb(d):
    b = InlineKeyboardBuilder()
    for i in range(len(d['photos'])):
        b.button(text=f'❌ {i + 1}', callback_data=f'd:rm:{i}')
    for i in range(1, len(d['photos'])):
        b.button(text=f'⬆️ {i + 1}', callback_data=f'd:first:{i}')
    b.button(text='🗑 Удалить все', callback_data='d:clear')
    if d['site_photos']:
        b.button(text='🌐 Фото с сайта', callback_data='d:site')
    b.button(text='⬅️ Назад', callback_data='d:back')
    n = len(d['photos'])
    b.adjust(*([5] * ((n + 4) // 5)), *([5] * ((max(n - 1, 0) + 4) // 5)), 2, 1)
    return b.as_markup()


def control_text(d):
    lines = ['☝️ <b>Так будет выглядеть пост</b>']
    missing = [WARN_NAMES[k] for k in ('title', 'price', 'photos') if not d[k]]
    if d['full']:
        pass
    elif d['blocked'] and len(missing) == 3:
        lines.append(f"\n⚠️ <b>{d['shop'] or 'Сайт'} не отдаёт данные боту</b> (защита от ботов).\n"
                     f"Заполни вручную: «✏️ Название», «✏️ Цена» и пришли скриншот товара.")
    elif d['from_link'] and len(missing) == 3:
        lines.append("\n⚠️ <b>Не удалось получить данные со страницы.</b>\n"
                     "Заполни вручную: «✏️ Название», «✏️ Цена» и пришли скриншот товара.")
    elif missing:
        lines.append(f"⚠️ Не найдено: {', '.join(missing)} — заполни вручную")
    if d['error']:
        lines.append(f"⚠️ {escape(d['error'])}")
    lines.append('\nСвои фото/скриншоты — просто отправь их сюда.')
    return '\n'.join(lines)


async def delete_preview(bot, chat_id, d):
    if d['msg_ids']:
        try:
            await bot.delete_messages(chat_id, d['msg_ids'])
        except Exception:
            pass
        d['msg_ids'] = []


PHOTOS_HINT = ('❌ N — удалить фото, ⬆️ N — сделать первым.\n'
               'Добавить свои — просто отправь их в чат.')


async def render(bot, chat_id, user_id, photos_menu=False):
    """Удаляет старый черновик в чате и отправляет новый."""
    d = drafts.get(user_id)
    if not d:
        return
    await delete_preview(bot, chat_id, d)
    messages, file_ids, d['error'] = await send_preview(bot, chat_id, build_text(d), d['photos'])
    d['photos'] = file_ids  # только реально загрузившиеся фото; публикация без повторной загрузки
    if photos_menu:
        ctrl = await bot.send_message(chat_id, PHOTOS_HINT, reply_markup=photos_kb(d))
    else:
        ctrl = await bot.send_message(chat_id, control_text(d), reply_markup=control_kb(d))
    d['msg_ids'] = [m.message_id for m in messages] + [ctrl.message_id]


def render_later(bot, chat_id, user_id, delay=1.5):
    """Альбом приходит несколькими сообщениями — перерисовываем один раз после последнего фото."""
    old = render_tasks.get(user_id)
    if old and not old.done():
        old.cancel()

    async def job():
        await asyncio.sleep(delay)
        await render(bot, chat_id, user_id)

    render_tasks[user_id] = asyncio.create_task(job())


# ---------- Доступ ----------

@rout.message(~is_admin)
async def no_access(message: Message):
    await message.answer(f'Нет доступа. Твой id: <code>{message.from_user.id}</code>\n'
                         f'Добавь его в ADMIN_IDS в .env и перезапусти бота.')


# ---------- Команды ----------

@rout.message(CommandStart())
async def start(message: Message):
    await message.answer('Отправь ссылку на товар — я подтяну название, цену и фото.\n'
                         'Можно сразу отправить фото — тогда создам пустой черновик.\n'
                         '/new — пустой черновик, /cancel — отменить.')


@rout.message(Command('new'))
async def cmd_new(message: Message, state: FSMContext):
    await state.clear()
    old = drafts.get(message.from_user.id)
    if old:
        await delete_preview(message.bot, message.chat.id, old)
    drafts[message.from_user.id] = new_draft()
    await render(message.bot, message.chat.id, message.from_user.id)


@rout.message(Command('cancel'))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    d = drafts.pop(message.from_user.id, None)
    if d:
        await delete_preview(message.bot, message.chat.id, d)
    await message.answer('Черновик удалён.')


# ---------- Ввод значения поля ----------

@rout.message(Edit.waiting, F.text)
async def field_input(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get('field')
    d = drafts.get(message.from_user.id)
    await state.set_state(None)
    try:
        await message.bot.delete_message(message.chat.id, data.get('prompt_id'))
    except Exception:
        pass
    if not d or not field:
        return

    if field == 'price':
        value = re.sub(r'(руб\.?|₽|р\.)', '', message.text, flags=re.I).strip()
        d['price'] = escape(value)
    elif field == 'link':
        d['link'] = escape(message.text.strip())
    else:
        d[field] = message.html_text.strip()  # сохраняем форматирование: жирный, курсив, ссылки
    await render(message.bot, message.chat.id, message.from_user.id)


# ---------- Ссылка → новый черновик ----------

@rout.message(F.text.regexp(URL_RE))
async def new_from_link(message: Message, state: FSMContext):
    await state.clear()
    url = URL_RE.search(message.text).group(0)
    wait = await message.answer('⏳ Смотрю товар…')

    parsed = await parse(url)
    old = drafts.get(message.from_user.id)
    if old:
        await delete_preview(message.bot, message.chat.id, old)
    drafts[message.from_user.id] = new_draft(parsed)

    await wait.delete()
    await render(message.bot, message.chat.id, message.from_user.id)


# ---------- Свои фото ----------

@rout.message(F.photo)
async def add_photo(message: Message):
    d = drafts.setdefault(message.from_user.id, new_draft())
    if len(d['photos']) >= MAX_PHOTOS:
        await message.answer(f'Максимум {MAX_PHOTOS} фото — удали лишние в меню «🖼 Фото».')
        return
    d['photos'].append(message.photo[-1].file_id)
    render_later(message.bot, message.chat.id, message.from_user.id)


@rout.message()
async def other(message: Message):
    await message.answer('Отправь ссылку на товар или фото. Править черновик — кнопками под ним.')


# ---------- Кнопки черновика ----------

async def safe_answer(cb, text=None, show_alert=False):
    try:
        await cb.answer(text, show_alert=show_alert)
    except Exception:
        pass  # кнопка просто дольше покажет «часики» — не страшно


@rout.callback_query(F.data.startswith('d:'), is_admin)
async def draft_buttons(cb: CallbackQuery, state: FSMContext):
    user_id, chat_id = cb.from_user.id, cb.message.chat.id
    d = drafts.get(user_id)
    action = cb.data[2:]
    if not d:
        await safe_answer(cb, 'Черновик устарел — отправь ссылку заново', show_alert=True)
        return
    # Отвечаем на нажатие сразу: Telegram ждёт ответ ~15 секунд, а перерисовка может идти дольше
    await safe_answer(cb)

    if action in FIELDS:
        await state.set_state(Edit.waiting)
        b = InlineKeyboardBuilder()
        b.button(text='Отмена', callback_data='d:noedit')
        prompt = await cb.message.answer(f'Отправь {FIELDS[action]}.\n'
                                         f'Можно с форматированием (жирный, курсив, ссылка).',
                                         reply_markup=b.as_markup())
        await state.update_data(field=action, prompt_id=prompt.message_id)
        return

    if action == 'noedit':
        await state.set_state(None)
        await cb.message.delete()
    elif action == 'unfull':
        d['full'] = ''
        await render(cb.bot, chat_id, user_id)
    elif action == 'photos':
        await cb.message.edit_text(PHOTOS_HINT, reply_markup=photos_kb(d))
    elif action == 'back':
        await cb.message.edit_text(control_text(d), reply_markup=control_kb(d))
    elif action.startswith('rm:'):
        i = int(action[3:])
        if i < len(d['photos']):
            d['photos'].pop(i)
        await render(cb.bot, chat_id, user_id, photos_menu=True)
    elif action.startswith('first:'):
        i = int(action[6:])
        if i < len(d['photos']):
            d['photos'].insert(0, d['photos'].pop(i))
        await render(cb.bot, chat_id, user_id, photos_menu=True)
    elif action == 'clear':
        d['photos'] = []
        await render(cb.bot, chat_id, user_id, photos_menu=True)
    elif action == 'site':
        d['photos'] = list(d['site_photos'])
        await render(cb.bot, chat_id, user_id, photos_menu=True)
    elif action == 'cancel':
        await state.clear()
        await delete_preview(cb.bot, chat_id, d)
        drafts.pop(user_id, None)
        await cb.message.answer('Черновик удалён.')
    elif action == 'pub':
        await publish(cb, d)


@rout.callback_query(F.data.startswith('d:'))
async def draft_no_access(cb: CallbackQuery):
    await cb.answer('Нет доступа', show_alert=True)


async def publish(cb: CallbackQuery, d):
    post_id = f'm{int(time.time() * 1000)}'  # id для кнопки «Товар закончился?»
    try:
        await send_post(cb.bot, channel_id, build_text(d), d['photos'], await kb.sold_out(post_id))
    except Exception as e:
        logging.error(f'Не удалось опубликовать: {e}')
        await cb.message.answer(f'❌ Не удалось опубликовать: {escape(str(e))}\nНажми «Опубликовать» ещё раз.')
        return
    await db.insert_vk_id(post_id)
    drafts.pop(cb.from_user.id, None)
    await cb.message.edit_text('✅ Опубликовано в канал')

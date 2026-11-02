import asyncio
import requests
import vk_api
from config_reader import config

import db

token = config.vk_api_token.get_secret_value()


class TimeoutSession(requests.Session):
    # vk_api по умолчанию ждёт ответа бесконечно: при обрыве интернета запрос зависал навсегда
    def request(self, *args, **kwargs):
        kwargs.setdefault('timeout', 15)
        return super().request(*args, **kwargs)


vk_session = vk_api.VkApi(token=token, session=TimeoutSession())
vk = vk_session.get_api()

group_id = 'salechest'

def get_latest_post(domain):
    try:
        response = vk.wall.get(domain=domain, count=1)
        if response['items']:
            return response['items'][0]
    except Exception as e:
        print(e)
        print("Ошибка в запросе к стене")
    return None

def get_photos_from_post(post):
    photos = []
    for attachment in post.get('attachments', []):
        if attachment['type'] == 'photo':
            photo = attachment['photo']
            photo_url = max(photo['sizes'], key=lambda size: size['width'])['url']
            photos.append(photo_url)
    return photos

async def get_new_post():
    latest_post_id = await db.get_last_id()
    new_post = await asyncio.to_thread(get_latest_post, group_id)

    if new_post and new_post['id'] > int(latest_post_id):
        # В БД пост записывается в main.py — только после успешной отправки в Telegram
        url_photos = get_photos_from_post(new_post)
        text = new_post['text']
        post = {'id':str(new_post['id']), 'text': text, 'photos': url_photos}
        return post
    else:
        return False

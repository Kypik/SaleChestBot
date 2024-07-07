import vk_api
from config_reader import config

import db

token = config.vk_api_token.get_secret_value()
vk_session = vk_api.VkApi(token=token)
vk = vk_session.get_api()

group_id = 'salechest'  # 'club226456228'

def get_latest_post(owner_id):
    response = vk.wall.get(owner_id=owner_id, count=1)
    if response['items']:
        return response['items'][0]
    return None

def get_photos_from_post(post):
    if 'attachments' in post:
        photos = []
        for attachment in post['attachments']:
            if attachment['type'] == 'photo':
                photo = attachment['photo']
                photo_url = max(photo['sizes'], key=lambda size: size['width'])['url']
                photos.append(photo_url)
        return photos
    else:
        print('В посте нет вложений')

async def get_new_post():
    latest_post_id = await db.get_last_id()
    new_post = get_latest_post(group_id)

    if new_post and new_post['id'] > int(latest_post_id):
        await db.insert_vk_id(new_post['id'])
        url_photos = get_photos_from_post(new_post)
        text = new_post['text']
        post = {'id':str(new_post['id']), 'text': text, 'photos': url_photos}
        return post
    else:
        return False

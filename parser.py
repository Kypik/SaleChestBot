import asyncio
import json
import logging
import os
import re
from html import escape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

# Запросы к магазинам идут напрямую с домашнего IP (не через прокси Telegram).
# curl_cffi притворяется браузером Chrome на уровне TLS — иначе антиботы WB/Ozon
# отдают заглушку вместо данных
HEADERS = {'Accept-Language': 'ru-RU,ru;q=0.9'}
MAX_PHOTOS = 10


def new_session():
    return AsyncSession(impersonate='chrome', headers=HEADERS, timeout=20)


async def http_get(session, url, **kw):
    try:
        return await session.get(url, **kw)
    except Exception as e:
        # Некоторые сайты (например ali.click) отдают неполную цепочку сертификатов:
        # браузер её достраивает, а curl — нет. Страницу только читаем, поэтому повторяем без проверки
        if 'certificate' in str(e).lower() or 'ssl' in str(e).lower():
            logging.warning(f'{url}: проблема с сертификатом сайта, повторяем без проверки')
            return await session.get(url, verify=False, **kw)
        raise

SHOPS = [
    (('wildberries.', 'wb.ru'), 'Wb'),
    (('ozon.',), 'Ozon'),
    (('aliexpress.', 'ali.click', 'aliclick.'), 'AliExpress'),
    (('market.yandex.',), 'Яндекс Маркет'),
    (('megamarket.',), 'Мегамаркет'),
]

# Эти магазины всегда отвечают боту капчей — страницу даже не запрашиваем, сразу ручной ввод
MANUAL_SHOPS = ('Ozon', 'AliExpress')

# Заголовки страниц-заглушек антибота — такое название не берём
BAD_TITLES = ('доступ ограничен', 'antibot', 'captcha', 'проверка', 'access denied', 'just a moment')


def detect_shop(url):
    host = urlparse(url).netloc.lower()
    for keys, name in SHOPS:
        if any(k in host for k in keys):
            return name
    return host.removeprefix('www.')


def clean_title(title):
    title = ' '.join(title.split())
    title = re.split(r'\s+купить\s', title, maxsplit=1, flags=re.I)[0]
    title = re.sub(r'\s*[-|—]\s*(AliExpress|Wildberries|OZON|Ozon)[^-|—]*$', '', title, flags=re.I)
    if any(b in title.lower() for b in BAD_TITLES):
        return ''
    return title.strip()


def format_price(value):
    try:
        price = float(str(value).replace(' ', '').replace(',', '.'))
    except ValueError:
        return ''
    if price <= 0:
        return ''
    return str(int(price)) if price == int(price) else f'{price:.2f}'


# ---------- Wildberries ----------

# Верхние границы vol для basket-01, basket-02, ... (WB периодически добавляет новые)
WB_BASKETS = [143, 287, 431, 719, 1007, 1061, 1115, 1169, 1313, 1601, 1655, 1919, 2045, 2189,
              2405, 2621, 2837, 3053, 3269, 3485, 3701, 3917, 4133, 4349, 4565]


def wb_id(url):
    m = re.search(r'/catalog/(\d+)', url)
    return int(m.group(1)) if m else None


async def wb_card(session, host):
    resp = await http_get(session, f'{host}/info/ru/card.json', timeout=7)
    if resp.status_code != 200:
        return None
    return resp.json()


async def wb_find_basket(session, nm):
    vol, part = nm // 100000, nm // 1000
    make = lambda b: f'https://basket-{b:02d}.wbbasket.ru/vol{vol}/part{part}/{nm}'

    guess = next((i + 1 for i, top in enumerate(WB_BASKETS) if vol <= top), None)
    if guess:
        try:
            card = await wb_card(session, make(guess))
            if card:
                return make(guess), card
        except Exception:
            pass

    # Не угадали — перебираем все корзины параллельно
    async def probe(b):
        try:
            card = await wb_card(session, make(b))
            return (make(b), card) if card else None
        except Exception:
            return None

    tasks = [asyncio.create_task(probe(b)) for b in range(1, 41)]
    try:
        for done in asyncio.as_completed(tasks):
            found = await done
            if found:
                return found
    finally:
        for t in tasks:
            t.cancel()
    return None, None


async def wb_price(session, nm):
    # С большинства IP WB отдаёт цену пустой/403 (антибот), тогда цену вводят вручную
    for ver in ('v4',):
        url = (f'https://card.wb.ru/cards/{ver}/detail'
               f'?appType=1&curr=rub&dest=-1257786&spp=30&nm={nm}')
        try:
            data = (await http_get(session, url)).json()
            products = data.get('products') or data.get('data', {}).get('products') or []
            if not products:
                continue
            product = products[0]
            for size in product.get('sizes', []):
                price = size.get('price', {}).get('product')
                if price:
                    return format_price(price / 100)
            if product.get('salePriceU'):
                return format_price(product['salePriceU'] / 100)
        except Exception:
            logging.info('WB не отдал цену (антибот) — вводится вручную')
    return ''


async def parse_wb(session, nm):
    host, card = await wb_find_basket(session, nm)
    result = {'title': '', 'price': '', 'photos': []}
    if card:
        result['title'] = card.get('imt_name') or card.get('subj_name') or ''
        count = min(card.get('media', {}).get('photo_count', 0), MAX_PHOTOS)
        result['photos'] = [f'{host}/images/big/{i}.webp' for i in range(1, count + 1)]
    result['price'] = await wb_price(session, nm)
    return result


# ---------- Любой сайт: og-теги, JSON-LD ----------

def parse_html(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')

    def meta(*names):
        for n in names:
            tag = soup.find('meta', attrs={'property': n}) or soup.find('meta', attrs={'name': n})
            if tag and tag.get('content'):
                return tag['content'].strip()
        return ''

    title, price, photos = meta('og:title', 'twitter:title'), '', []

    for tag in soup.find_all('meta', attrs={'property': 'og:image'}):
        if tag.get('content'):
            photos.append(tag['content'])

    # JSON-LD с типом Product
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or '')
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get('@graph', [data])
        for item in items:
            if not isinstance(item, dict) or 'Product' not in str(item.get('@type')):
                continue
            title = title or item.get('name', '')
            images = item.get('image') or []
            images = [images] if isinstance(images, (str, dict)) else images
            photos += [i.get('url') if isinstance(i, dict) else i for i in images]
            offers = item.get('offers') or {}
            offers = offers[0] if isinstance(offers, list) and offers else offers
            if isinstance(offers, dict):
                price = price or format_price(offers.get('price') or offers.get('lowPrice') or '')

    price = price or format_price(meta('product:price:amount', 'og:price:amount'))
    if not price:
        tag = soup.find(attrs={'itemprop': 'price'})
        if tag:
            price = format_price(tag.get('content') or tag.get_text())

    if not title and soup.title:
        title = soup.title.get_text()

    clean = []
    for p in photos:
        if not p:
            continue
        p = urljoin(page_url, 'https:' + p if p.startswith('//') else p)
        if p not in clean:
            clean.append(p)

    return {'title': clean_title(title), 'price': price, 'photos': clean[:MAX_PHOTOS]}


# ---------- Точка входа ----------

def save_debug(shop, html):
    """Сохраняет страницу, на которой ничего не нашлось, — чтобы понять, что отдал сайт."""
    try:
        os.makedirs('debug', exist_ok=True)
        name = re.sub(r'[^\w.-]', '_', shop) or 'page'
        with open(os.path.join('debug', f'{name}.html'), 'w', encoding='utf-8') as f:
            f.write(html)
        logging.info(f'Ничего не найдено — страница сохранена в debug/{name}.html')
    except Exception:
        pass


async def parse(url):
    """
    Возвращает {'shop','title','price','photos','link','blocked'}; title/shop уже экранированы под HTML.
    blocked=True — сайт не отдаёт данные боту (капча/антибот), всё заполняется вручную.
    """
    result = {'shop': detect_shop(url), 'title': '', 'price': '', 'photos': [], 'link': url, 'blocked': False}
    if result['shop'] in MANUAL_SHOPS:
        result['blocked'] = True
        return result

    data = {}
    try:
        async with new_session() as session:
            nm = wb_id(url)
            if nm:
                data = await parse_wb(session, nm)
            else:
                resp = await http_get(session, url, allow_redirects=True)
                final_url, html = str(resp.url), resp.text
                page_title = BeautifulSoup(html, 'html.parser').title
                logging.info(f'{url} -> HTTP {resp.status_code}, {final_url}, {len(html)} байт, '
                             f'title: {page_title.get_text(strip=True)[:80] if page_title else "-"}')
                if result['shop'] not in [name for _, name in SHOPS]:
                    result['shop'] = detect_shop(final_url)  # короткая ссылка → настоящий магазин
                title_text = page_title.get_text().lower() if page_title else ''
                if result['shop'] in MANUAL_SHOPS or resp.status_code in (403, 429, 498) \
                        or any(b in title_text for b in BAD_TITLES):
                    result['blocked'] = True
                    result['shop'] = escape(result['shop'])
                    return result
                nm = wb_id(final_url)
                data = await parse_wb(session, nm) if nm and 'wildberries' in final_url else parse_html(html, final_url)
        if not data.get('title') and not nm:
            save_debug(result['shop'], html)
    except Exception as e:
        logging.warning(f'Не удалось разобрать {url}: {e}')

    result.update({k: v for k, v in data.items() if v})
    result['shop'] = escape(result['shop'])
    result['title'] = escape(result['title'])
    return result

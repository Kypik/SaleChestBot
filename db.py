import aiosqlite
import json

async def creat_table():
    async with aiosqlite.connect('salechest.sql') as db:
        await db.execute("CREATE TABLE IF NOT EXISTS posts(id_vk TEXT, id_tg INT, click_count INT, list_users_id TEXT)")
        await db.commit()

async def insert_vk_id(value):
    async with aiosqlite.connect('salechest.sql') as db:
        await db.execute("INSERT INTO posts (id_vk, click_count) VALUES (?, ?)", (value, 0))
        await db.commit()

async def insert_value_to_vk_id(parameter, value, id_vk):
    async with aiosqlite.connect('salechest.sql') as db:
        await db.execute(f"UPDATE posts SET {parameter} = ? WHERE id_vk = ?", (value, id_vk))
        await db.commit()

async def get_value(parameter, id_vk):
    async with aiosqlite.connect('salechest.sql') as db:
        async with db.execute(f"SELECT {parameter} FROM posts WHERE id_vk = ?", (id_vk,)) as cursor:
            value = await cursor.fetchone()
            return value

async def get_last_id():
    async with aiosqlite.connect('salechest.sql') as db:
        async with db.execute("SELECT * FROM posts") as cursor:
            list_id = []
            for row in await cursor.fetchall():
                for el in row:
                    list_id.append(el)
                    break

            if len(list_id) > 100:
                await db.execute("DELETE FROM posts WHERE ROWID IN (SELECT ROWID FROM posts ORDER BY ROWID LIMIT 50)")
                await db.commit()

            try:
                return list_id[-1]
            except:
                return 0
            
async def insert_list_user_id(list_id, id_vk):
    list_id = json.dumps(list_id,)
    async with aiosqlite.connect('salechest.sql') as db:
        await db.execute("UPDATE posts SET list_users_id = ? WHERE id_vk = ?", (list_id, id_vk))
        await db.commit()

async def get_list_user_id(id_vk):
    async with aiosqlite.connect('salechest.sql') as db:
        async with db.execute("SELECT list_users_id FROM posts WHERE id_vk = ?", (id_vk,)) as cursor:
            list_id = await cursor.fetchone()
            try:
                list_id = json.loads(list_id[0])
                return(list_id)
            except:
                return []
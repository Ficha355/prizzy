import asyncio
import aiohttp

PING_URL = "https://prizzy.onrender.com/ping"
INTERVAL = 300  # 5 minutes


async def start():
    await asyncio.sleep(30)  # attendre que gunicorn soit prêt
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(PING_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    print(f"[KEEPALIVE] {PING_URL} → {resp.status}")
            except Exception as exc:
                print(f"[KEEPALIVE] erreur : {exc}")
            await asyncio.sleep(INTERVAL)

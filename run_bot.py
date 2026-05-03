from dotenv import load_dotenv
load_dotenv()

import os
import asyncio
import bot
import keepalive

token = os.environ.get("DISCORD_BOT_TOKEN")
if not token:
    raise RuntimeError("DISCORD_BOT_TOKEN manquant dans le .env")


async def main():
    asyncio.create_task(keepalive.start())
    await bot.client.start(token)


asyncio.run(main())

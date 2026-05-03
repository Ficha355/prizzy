from dotenv import load_dotenv
load_dotenv()

import os
import bot

token = os.environ.get("DISCORD_BOT_TOKEN")
if not token:
    raise RuntimeError("DISCORD_BOT_TOKEN manquant dans le .env")

bot.client.run(token)

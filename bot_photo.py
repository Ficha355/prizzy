from dotenv import load_dotenv
load_dotenv()

import os
import asyncio
import aiohttp
import discord

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
API_URL      = "https://prizzy.onrender.com/photo-ia"
ADMIN_TOKEN  = os.environ.get("ADMIN_TOKEN", "")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def _download(url: str, session: aiohttp.ClientSession) -> bytes:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


@client.event
async def on_ready():
    print(f"[PHOTO] Bot connecté : {client.user} (id={client.user.id})")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    images = [
        a for a in message.attachments
        if (a.content_type or "").split(";")[0].strip() in ALLOWED_MIME
    ]
    if not images:
        return

    att = images[0]

    status_msg = await message.reply("✨ Amélioration de la photo en cours…", mention_author=False)

    try:
        async with aiohttp.ClientSession() as http:
            img_bytes = await _download(att.url, http)

            form = aiohttp.FormData()
            form.add_field(
                "image",
                img_bytes,
                filename=att.filename or "image.jpg",
                content_type=(att.content_type or "image/jpeg").split(";")[0].strip(),
            )

            async with http.post(
                API_URL,
                data=form,
                headers={"X-Bot-Token": ADMIN_TOKEN},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    await status_msg.edit(content=f"❌ Erreur API ({resp.status}) : {text[:200]}")
                    return
                result = await resp.json()

        image_url = result.get("image_url", "")
        if not image_url:
            await status_msg.edit(content="❌ L'API n'a pas retourné d'image.")
            return

        # image_url peut être un chemin relatif /uploads/...
        if image_url.startswith("/"):
            image_url = "https://prizzy.onrender.com" + image_url

        embed = discord.Embed(
            title="✨ Photo améliorée par Prizzy IA",
            color=discord.Color.from_rgb(234, 88, 12),
        )
        embed.set_image(url=image_url)
        embed.set_footer(text="Prizzy Photo IA · GPT-4o")

        await status_msg.edit(content=None, embed=embed)

    except asyncio.TimeoutError:
        await status_msg.edit(content="❌ Délai dépassé (>120s). Réessaie avec une image plus petite.")
    except Exception as exc:
        await status_msg.edit(content=f"❌ Erreur : {exc}")


token = os.environ.get("DISCORD_PHOTO_BOT_TOKEN")
if not token:
    raise RuntimeError("DISCORD_PHOTO_BOT_TOKEN manquant dans le .env")

asyncio.run(client.start(token))

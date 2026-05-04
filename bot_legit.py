from dotenv import load_dotenv
load_dotenv()

import os
import asyncio
import aiohttp
import discord

from claude_client import legit_check

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def _download(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


@client.event
async def on_ready():
    print(f"[LEGIT] Bot connecté : {client.user} (id={client.user.id})")


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

    oversized = [a for a in images if a.size > 5_242_880]
    if oversized:
        names = ", ".join(a.filename for a in oversized)
        await message.reply(
            f"⚠️ Image trop lourde ({names}). Merci d'envoyer une photo de moins de 5 MB.",
            mention_author=False,
        )
        return

    status_msg = await message.reply("⏳ Analyse en cours…", mention_author=False)

    try:
        image_data: list[tuple[bytes, str]] = []
        for att in images[:5]:
            mime = (att.content_type or "image/jpeg").split(";")[0].strip()
            data = await _download(att.url)
            image_data.append((data, mime))

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, legit_check, image_data)

        verdict = result.get("verdict", "Inconnu")
        confiance = result.get("confiance", 0)
        marque = result.get("marque_detectee") or "Non identifiée"
        details = result.get("details", [])
        resume = result.get("resume", "")

        if verdict == "Authentique":
            color = discord.Color.green()
            verdict_emoji = "✅"
        elif verdict == "Suspect":
            color = discord.Color.orange()
            verdict_emoji = "⚠️"
        else:
            color = discord.Color.red()
            verdict_emoji = "❌"

        confiance_dot = "🟢" if confiance >= 80 else ("🟠" if confiance >= 50 else "🔴")

        embed = discord.Embed(
            title=f"{verdict_emoji} Legit Check — {marque}",
            description=resume,
            color=color,
        )
        embed.add_field(name="Verdict", value=f"**{verdict}**", inline=True)
        embed.add_field(
            name="Score de confiance",
            value=f"{confiance_dot} **{confiance}/100**",
            inline=True,
        )
        if details:
            embed.add_field(
                name="Observations",
                value="\n".join(f"• {d}" for d in details[:5]),
                inline=False,
            )
        embed.set_footer(text="Prizzy Legit Check IA")

        await status_msg.edit(content=None, embed=embed)

    except Exception as exc:
        await status_msg.edit(content=f"❌ Erreur lors de l'analyse : {exc}")


token = os.environ.get("DISCORD_LEGIT_BOT_TOKEN")
if not token:
    raise RuntimeError("DISCORD_LEGIT_BOT_TOKEN manquant dans le .env")

asyncio.run(client.start(token))

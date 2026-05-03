from dotenv import load_dotenv
load_dotenv()

import os
import asyncio
import statistics
import aiohttp
import discord

from claude_client import analyze_clothing
from vinted_client import search_items

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_SCORE_LABELS = [
    (8, "Forte demande, peu de concurrence — excellente semaine pour vendre"),
    (6, "Bon moment pour publier — marché actif avec quelques concurrents"),
    (4, "Marché modéré — soignez votre description et vos photos"),
    (0, "Forte concurrence — misez sur un prix attractif et de belles photos"),
]

_SCORE_EMOJI = {range(0, 4): "🔴", range(4, 7): "🟠", range(7, 11): "🟢"}


def _score_emoji(score: int) -> str:
    for r, emoji in _SCORE_EMOJI.items():
        if score in r:
            return emoji
    return ""


def _compute_price_stats(items: list[dict]) -> dict:
    prices = [float(i["price_amount"]) for i in items if i.get("price_amount") is not None]
    if not prices:
        return {}
    sorted_p = sorted(prices)
    n = len(sorted_p)
    q1 = statistics.quantiles(sorted_p, n=4)[0] if n >= 4 else sorted_p[0]
    return {
        "count": n,
        "mean": round(statistics.mean(sorted_p), 2),
        "median": round(statistics.median(sorted_p), 2),
        "min": round(sorted_p[0], 2),
        "max": round(sorted_p[-1], 2),
        "recommended_price_7j": round(q1, 2),
    }


def _compute_sale_score(price_stats: dict, total_entries: int) -> dict:
    if not price_stats or price_stats.get("count", 0) < 3:
        return {"score": 5, "label": "Données insuffisantes pour calculer un score"}

    n = price_stats["count"]
    mean = price_stats["mean"]
    rec = price_stats["recommended_price_7j"]
    min_p = price_stats["min"]

    if total_entries < 30:       comp = 4
    elif total_entries < 100:    comp = 3
    elif total_entries < 300:    comp = 2
    elif total_entries < 800:    comp = 1
    else:                        comp = 0

    ratio = rec / mean if mean > 0 else 1.0
    if ratio < 0.55:     price = 3
    elif ratio < 0.70:   price = 2
    elif ratio < 0.85:   price = 1
    else:                price = 0

    spread = (mean - min_p) / min_p if min_p > 0 else 0
    if n >= 30 and spread > 0.5:     vitality = 3
    elif n >= 20 or spread > 0.3:    vitality = 2
    elif n >= 10:                    vitality = 1
    else:                            vitality = 0

    score = min(10, comp + price + vitality)
    label = next(lbl for threshold, lbl in _SCORE_LABELS if score >= threshold)
    return {"score": score, "label": label}


def _best_publish_time(price_stats: dict) -> dict:
    rec = price_stats.get("recommended_price_7j", 0) if price_stats else 0
    if rec > 50:
        return {"jour": "Jeudi", "creneau": "19h – 21h"}
    elif rec > 20:
        return {"jour": "Dimanche", "creneau": "18h – 20h"}
    else:
        return {"jour": "Samedi", "creneau": "10h – 12h"}


async def _download(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"[READY] Prizzy bot connecté : {client.user} (id={client.user.id})")
    print(f"[READY] Serveurs accessibles : {len(client.guilds)}")
    for guild in client.guilds:
        print(f"  [GUILD] {guild.name} (id={guild.id})")
        for channel in guild.text_channels:
            print(f"    [CHANNEL] #{channel.name} (id={channel.id})")


@client.event
async def on_message(message: discord.Message):
    print(
        f"[MSG] auteur={message.author} bot={message.author.bot} "
        f"salon={message.channel.id} pièces_jointes={len(message.attachments)}"
    )

    if message.author.bot:
        print("[SKIP] message d'un bot")
        return

    for att in message.attachments:
        print(f"[ATT] nom={att.filename} content_type={att.content_type} url={att.url}")

    images_attachments = [
        a for a in message.attachments
        if (a.content_type or "").split(";")[0].strip() in ALLOWED_MIME
    ]
    if not images_attachments:
        print("[SKIP] aucune image valide dans les pièces jointes")
        return

    print(f"[OK] {len(images_attachments)} image(s) détectée(s), lancement de l'analyse")

    status_msg = await message.reply("⏳ Analyse en cours…", mention_author=False)

    try:
        mime_map = {
            "image/jpeg": "image/jpeg",
            "image/png": "image/png",
            "image/webp": "image/webp",
            "image/gif": "image/jpeg",
        }

        images: list[tuple[bytes, str]] = []
        for att in images_attachments[:5]:
            mime = (att.content_type or "image/jpeg").split(";")[0].strip()
            data = await _download(att.url)
            images.append((data, mime_map.get(mime, "image/jpeg")))

        description = message.content.strip() or None

        loop = asyncio.get_running_loop()
        analysis = await loop.run_in_executor(None, analyze_clothing, images, description)

        vinted_query = (analysis.get("query_vinted") or "").strip()
        if not vinted_query:
            await status_msg.edit(content="❌ Impossible de construire une requête de recherche.")
            return

        vinted_results = await loop.run_in_executor(
            None, lambda: search_items(vinted_query, per_page=50, page=1)
        )

        price_stats = _compute_price_stats(vinted_results["items"])
        total_entries = vinted_results.get("total_entries", 0)
        sale_score = _compute_sale_score(price_stats, total_entries)
        publish_time = _best_publish_time(price_stats)

        score = sale_score["score"]
        emoji = _score_emoji(score)

        rec_price = price_stats.get("recommended_price_7j")
        price_line = f"**{rec_price} €**" if rec_price else "Données insuffisantes"

        embed = discord.Embed(
            title="📦 Analyse Prizzy",
            color=discord.Color.from_str("#6C5CE7"),
        )

        info_parts = []
        if analysis.get("marque"):
            info_parts.append(f"**Marque :** {analysis['marque']}")
        if analysis.get("type_vetement"):
            info_parts.append(f"**Type :** {analysis['type_vetement']}")
        if analysis.get("taille"):
            info_parts.append(f"**Taille :** {analysis['taille']}")
        if analysis.get("etat_estime"):
            info_parts.append(f"**État :** {analysis['etat_estime']}")
        if info_parts:
            embed.add_field(name="Vêtement identifié", value="\n".join(info_parts), inline=False)

        embed.add_field(name="💰 Prix recommandé (vente en 7 j)", value=price_line, inline=True)
        embed.add_field(
            name=f"📊 Score de vente",
            value=f"{emoji} **{score}/10**\n{sale_score['label']}",
            inline=False,
        )
        embed.add_field(
            name="🕐 Meilleur moment pour publier",
            value=f"**{publish_time['jour']}** · {publish_time['creneau']}",
            inline=True,
        )

        description_text = analysis.get("description_optimisee", "")
        if description_text:
            embed.add_field(
                name="✍️ Description optimisée",
                value=description_text[:1024],
                inline=False,
            )

        if price_stats:
            embed.set_footer(
                text=f"Basé sur {price_stats['count']} annonces · "
                     f"Prix moyen {price_stats['mean']} € · "
                     f"Recherche : {vinted_query}"
            )

        await status_msg.edit(content=None, embed=embed)

    except Exception as exc:
        await status_msg.edit(content=f"❌ Erreur : {exc}")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN manquant dans le .env")
    client.run(token)

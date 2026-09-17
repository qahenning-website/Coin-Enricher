"""
Coin Enricher Bot
Watches one Discord channel for Solana contract addresses
and replies once with launch time, top 10 holder %, and dev holding %.
"""

import os
import re
import asyncio
from datetime import datetime, timezone

import discord
import requests
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
BIRDEYE_API_KEY = os.environ["BIRDEYE_API_KEY"]
WATCH_CHANNEL_ID = int(os.environ["WATCH_CHANNEL_ID"])

SOL_CA_REGEX = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

BIRDEYE_HEADERS = {
    "X-API-KEY": BIRDEYE_API_KEY,
    "x-chain": "solana",
}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def extract_ca(text: str) -> str | None:
    candidates = SOL_CA_REGEX.findall(text)
    for c in candidates:
        if any(ch.isdigit() for ch in c):
            return c
    return candidates[0] if candidates else None


def get_token_overview(ca: str) -> dict | None:
    url = "https://public-api.birdeye.so/defi/token_overview"
    try:
        r = requests.get(url, headers=BIRDEYE_HEADERS, params={"address": ca}, timeout=10)
        r.raise_for_status()
        return r.json().get("data")
    except Exception as e:
        print(f"[overview error] {ca}: {e}")
        return None


def get_token_creation_info(ca: str) -> dict | None:
    url = "https://public-api.birdeye.so/defi/token_creation_info"
    try:
        r = requests.get(url, headers=BIRDEYE_HEADERS, params={"address": ca}, timeout=10)
        r.raise_for_status()
        return r.json().get("data")
    except Exception as e:
        print(f"[creation info error] {ca}: {e}")
        return None


def get_holders(ca: str, limit: int = 20) -> list:
    url = "https://public-api.birdeye.so/defi/v3/token/holder"
    try:
        r = requests.get(
            url,
            headers=BIRDEYE_HEADERS,
            params={"address": ca, "offset": 0, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("data", {}).get("items", [])
    except Exception as e:
        print(f"[holders error] {ca}: {e}")
        return []


def build_enrichment_embed(ca: str) -> discord.Embed | None:
    overview = get_token_overview(ca)
    creation = get_token_creation_info(ca)
    holders = get_holders(ca)

    if not overview and not holders:
        return None

    embed = discord.Embed(title="Extra Coin Info", color=0x5865F2)

    if creation and creation.get("blockUnixTime"):
        launched = datetime.fromtimestamp(creation["blockUnixTime"], tz=timezone.utc)
        age = datetime.now(timezone.utc) - launched
        hours = age.total_seconds() / 3600
        age_str = f"{hours:.1f}h ago" if hours < 48 else f"{hours / 24:.1f}d ago"
        embed.add_field(
            name="Launched",
            value=f"{launched.strftime('%Y-%m-%d %H:%M UTC')} ({age_str})",
            inline=False,
        )

    total_supply = None
    if overview:
        total_supply = overview.get("supply") or overview.get("totalSupply")

    if holders and total_supply:
        top10_amount = sum(h.get("amount", 0) for h in holders[:10])
        top10_pct = (top10_amount / total_supply) * 100
        embed.add_field(name="Top 10 Holders", value=f"{top10_pct:.1f}%", inline=True)

    dev_wallet = creation.get("creator") if creation else None
    if dev_wallet and holders and total_supply:
        dev_amount = next(
            (h.get("amount", 0) for h in holders if h.get("owner") == dev_wallet),
            0,
        )
        dev_pct = (dev_amount / total_supply) * 100
        embed.add_field(name="Dev Holding", value=f"{dev_pct:.2f}%", inline=True)
    elif dev_wallet:
        embed.add_field(
            name="Dev Wallet",
            value=f"`{dev_wallet[:6]}...{dev_wallet[-4:]}` (not in top holders)",
            inline=True,
        )

    embed.set_footer(text=f"CA: {ca[:6]}...{ca[-6:]} • via Birdeye")
    return embed


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return
    if message.channel.id != WATCH_CHANNEL_ID:
        return

    ca = extract_ca(message.content)
    if not ca:
        return

    await asyncio.sleep(1)
    embed = build_enrichment_embed(ca)
    if embed:
        await message.reply(embed=embed, mention_author=False)


client.run(DISCORD_BOT_TOKEN)

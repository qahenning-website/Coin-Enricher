"""
Coin Enricher Bot
Replies once under a forwarded CA with:
  - Launch time (Eastern)
  - Top 10 holder %
  - Dev holding %
  - Bubblemaps V2 link
"""

import os
import re
import time
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
import requests
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
BIRDEYE_API_KEY = os.environ["BIRDEYE_API_KEY"]
WATCH_CHANNEL_ID = int(os.environ["WATCH_CHANNEL_ID"])

SOL_CA_REGEX = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
EASTERN = ZoneInfo("America/New_York")

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


def birdeye_get(url: str, params: dict, label: str, ca: str):
    try:
        r = requests.get(url, headers=BIRDEYE_HEADERS, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("data")
    except Exception as e:
        print(f"[{label} error] {ca}: {e}")
        return None


def get_token_overview(ca: str):
    return birdeye_get(
        "https://public-api.birdeye.so/defi/token_overview",
        {"address": ca},
        "overview",
        ca,
    )


def get_token_creation_info(ca: str):
    return birdeye_get(
        "https://public-api.birdeye.so/defi/token_creation_info",
        {"address": ca},
        "creation info",
        ca,
    )


def get_holders(ca: str, limit: int = 20):
    data = birdeye_get(
        "https://public-api.birdeye.so/defi/v3/token/holder",
        {"address": ca, "offset": 0, "limit": limit},
        "holders",
        ca,
    )
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("items", []) or []


def get_dexscreener_created(ca: str) -> int | None:
    """Unix seconds from DexScreener pairCreatedAt."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{ca}",
            timeout=10,
        )
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        times = [p.get("pairCreatedAt") for p in pairs if p.get("pairCreatedAt")]
        if not times:
            return None
        ms = min(times)
        return int(ms / 1000) if ms > 10_000_000_000 else int(ms)
    except Exception as e:
        print(f"[dexscreener error] {ca}: {e}")
        return None


def get_pump_creator(ca: str) -> str | None:
    urls = [
        f"https://frontend-api.pump.fun/coins/{ca}",
        f"https://frontend-api-v3.pump.fun/coins/{ca}",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            creator = data.get("creator") or data.get("creatorAddress")
            if creator:
                return creator
        except Exception as e:
            print(f"[pump creator error] {ca}: {e}")
    return None


def holder_amount(h: dict) -> float:
    raw = h.get("ui_amount", h.get("uiAmount", h.get("amount", 0)))
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def holder_owner(h: dict) -> str:
    return (
        h.get("owner")
        or h.get("wallet")
        or h.get("address")
        or h.get("wallet_address")
        or ""
    )


def format_eastern(unix_seconds: int) -> tuple[str, str]:
    launched = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    launched_et = launched.astimezone(EASTERN)
    age = datetime.now(timezone.utc) - launched
    hours = age.total_seconds() / 3600
    age_str = f"{hours:.1f}h ago" if hours < 48 else f"{hours / 24:.1f}d ago"
    clock = launched_et.strftime("%Y-%m-%d %I:%M %p %Z")
    return clock, age_str


def build_enrichment_embed(ca: str) -> discord.Embed | None:
    overview = get_token_overview(ca)
    time.sleep(1.2)
    creation = get_token_creation_info(ca)
    time.sleep(1.2)
    holders = get_holders(ca)

    embed = discord.Embed(title="Extra Coin Info", color=0x5865F2)

    unix_time = None
    if creation:
        unix_time = creation.get("blockUnixTime") or creation.get("block_unix_time")
    if not unix_time:
        unix_time = get_dexscreener_created(ca)

    if unix_time:
        clock, age_str = format_eastern(int(unix_time))
        embed.add_field(
            name="Launched",
            value=f"{clock} ({age_str})",
            inline=False,
        )

    total_supply = None
    if overview:
        total_supply = (
            overview.get("supply")
            or overview.get("totalSupply")
            or overview.get("circulatingSupply")
        )
        try:
            total_supply = float(total_supply) if total_supply is not None else None
        except (TypeError, ValueError):
            total_supply = None

    if holders and total_supply:
        top10_amount = sum(holder_amount(h) for h in holders[:10])
        top10_pct = (top10_amount / total_supply) * 100
        embed.add_field(name="Top 10 Holders", value=f"{top10_pct:.1f}%", inline=True)
    elif holders:
        percents = []
        for h in holders[:10]:
            p = h.get("percentage") or h.get("percent") or h.get("ui_percentage")
            if p is not None:
                try:
                    percents.append(float(p))
                except (TypeError, ValueError):
                    pass
        if percents:
            embed.add_field(
                name="Top 10 Holders",
                value=f"{sum(percents):.1f}%",
                inline=True,
            )

    dev_wallet = None
    if creation:
        dev_wallet = creation.get("creator") or creation.get("owner")
    if not dev_wallet:
        dev_wallet = get_pump_creator(ca)

    if dev_wallet and holders and total_supply:
        dev_amount = next(
            (holder_amount(h) for h in holders if holder_owner(h) == dev_wallet),
            0,
        )
        dev_pct = (dev_amount / total_supply) * 100
        embed.add_field(name="Dev Holding", value=f"{dev_pct:.2f}%", inline=True)
    elif dev_wallet:
        embed.add_field(
            name="Dev Wallet",
            value=f"`{dev_wallet[:6]}...{dev_wallet[-4:]}`",
            inline=True,
        )

    return embed


def bubble_url(ca: str) -> str:
    return f"https://v2.bubblemaps.io/map?address={ca}&chain=solana"


def with_bubblemaps_link(text: str, ca: str) -> str:
    link = f"[Bubblemaps V2]({bubble_url(ca)})"
    raw = text or ""
    if "bubblemaps" in raw.lower():
        return raw
    if "DexTools" in raw:
        return raw.replace("DexTools", f"DexTools · {link}", 1)
    if "DexScreener" in raw:
        return raw.replace("DexScreener", f"DexScreener · {link}", 1)
    extra = f"\n[DexScreener](https://dexscreener.com/solana/{ca}) · [DexTools](https://www.dextools.io/app/en/solana/pair-explorer/{ca}) · {link}"
    return (raw + extra).strip()


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return
    if message.channel.id != WATCH_CHANNEL_ID:
        return

    source_text = message.content or ""
    if message.embeds:
        for e in message.embeds:
            source_text += " " + (e.title or "") + " " + (e.description or "")
            for f in e.fields:
                source_text += " " + (f.name or "") + " " + (f.value or "")

    ca = extract_ca(source_text) or extract_ca(message.content or "")
    if not ca:
        return

    await asyncio.sleep(1)
    extra = build_enrichment_embed(ca)
    if not extra:
        return

    combined = with_bubblemaps_link(message.content or "", ca)
    original_embeds = []
    for e in message.embeds:
        try:
            original_embeds.append(discord.Embed.from_dict(e.to_dict()))
        except Exception:
            pass
    embeds = (original_embeds + [extra])[:10]

    try:
        await message.delete()
        await message.channel.send(content=combined or None, embeds=embeds)
    except discord.Forbidden:
        extra.add_field(
            name="Bubblemaps V2",
            value=f"[Open map]({bubble_url(ca)})",
            inline=False,
        )
        await message.reply(embed=extra, mention_author=False)
    except Exception as e:
        print(f"[replace error] {e}")
        await message.reply(embed=extra, mention_author=False)


client.run(DISCORD_BOT_TOKEN)




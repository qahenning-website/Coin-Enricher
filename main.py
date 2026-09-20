"""
Coin Enricher Bot
Replaces the forwarded coin post with one message:
  original text + Bubblemaps link + Extra Coin Info embed
  (launched EST, top 10 %, dev holding %)
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
RPC_URL = "https://api.mainnet-beta.solana.com"

BIRDEYE_HEADERS = {
    "X-API-KEY": BIRDEYE_API_KEY,
    "x-chain": "solana",
}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def extract_ca(text: str):
    candidates = SOL_CA_REGEX.findall(text or "")
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


def rpc(method: str, params: list):
    try:
        r = requests.post(
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=12,
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"[rpc {method} error] {e}")
        return None


def get_token_overview(ca: str):
    return birdeye_get(
        "https://public-api.birdeye.so/defi/token_overview",
        {"address": ca},
        "overview",
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


def get_dexscreener_created(ca: str):
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


def get_pump_coin(ca: str):
    for url in (
        f"https://frontend-api.pump.fun/coins/{ca}",
        f"https://frontend-api-v3.pump.fun/coins/{ca}",
    ):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data:
                    return data
        except Exception as e:
            print(f"[pump error] {ca}: {e}")
    return None


def get_rugcheck(ca: str):
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{ca}/report", timeout=12)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[rugcheck error] {ca}: {e}")
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


def as_pct(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    if 0 < n <= 1:
        n *= 100
    return n


def parse_top10_from_text(text: str):
    if not text:
        return None
    m = re.search(r"top\s*10[^0-9%]{0,16}(\d+(?:\.\d+)?)\s*%", text, re.I)
    if not m:
        return None
    return as_pct(m.group(1))


def format_eastern(unix_seconds: int):
    launched = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    launched_et = launched.astimezone(EASTERN)
    age = datetime.now(timezone.utc) - launched
    hours = age.total_seconds() / 3600
    age_str = f"{hours:.1f}h ago" if hours < 48 else f"{hours / 24:.1f}d ago"
    clock = launched_et.strftime("%Y-%m-%d %I:%M %p") + " EST"
    return clock, age_str


def get_onchain_supply(ca: str):
    result = rpc("getTokenSupply", [ca])
    if not result:
        return None
    value = (result.get("value") or {}) if isinstance(result, dict) else {}
    ui = value.get("uiAmount")
    try:
        return float(ui) if ui is not None else None
    except (TypeError, ValueError):
        return None


def get_wallet_token_pct(wallet: str, mint: str, supply):
    result = rpc(
        "getTokenAccountsByOwner",
        [wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
    )
    if result is None:
        return None
    accounts = result.get("value") if isinstance(result, dict) else None
    if not accounts:
        return 0.0
    held = 0.0
    for acc in accounts:
        info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        tok = info.get("tokenAmount") or {}
        ui = tok.get("uiAmount")
        try:
            held += float(ui or 0)
        except (TypeError, ValueError):
            pass
    if not supply or supply <= 0:
        return 0.0 if held == 0 else None
    return (held / supply) * 100


def find_creator(ca: str, pump, rug):
    if pump:
        creator = pump.get("creator") or pump.get("creatorAddress")
        if creator:
            return creator
    if rug:
        creator = rug.get("creator") or (rug.get("token") or {}).get("creator")
        if creator:
            return creator
    return None


def build_enrichment_embed(ca: str, source_text: str = ""):
    overview = get_token_overview(ca)
    time.sleep(1.2)
    holders = get_holders(ca)
    if not holders:
        time.sleep(1.4)
        holders = get_holders(ca)

    pump = get_pump_coin(ca)
    rug = get_rugcheck(ca)

    embed = discord.Embed(title="Extra Coin Info", color=0x5865F2)

    unix_time = None
    if pump and pump.get("created_timestamp"):
        ts = pump["created_timestamp"]
        unix_time = int(ts / 1000) if ts > 10_000_000_000 else int(ts)
    if not unix_time:
        unix_time = get_dexscreener_created(ca)
    if unix_time:
        clock, age_str = format_eastern(int(unix_time))
        embed.add_field(name="Launched", value=f"{clock} ({age_str})", inline=False)

    total_supply = None
    if overview:
        raw_supply = (
            overview.get("supply")
            or overview.get("totalSupply")
            or overview.get("circulatingSupply")
        )
        try:
            total_supply = float(raw_supply) if raw_supply is not None else None
        except (TypeError, ValueError):
            total_supply = None
    if not total_supply:
        total_supply = get_onchain_supply(ca)

    top10_pct = None
    if holders and total_supply:
        top10_pct = (sum(holder_amount(h) for h in holders[:10]) / total_supply) * 100
    if top10_pct is None and holders:
        parts = []
        for h in holders[:10]:
            n = as_pct(h.get("percentage") or h.get("percent") or h.get("ui_percentage"))
            if n is not None:
                parts.append(n)
        if parts:
            top10_pct = sum(parts)
    if top10_pct is None and rug:
        top_list = rug.get("topHolders") or []
        if top_list:
            s = 0.0
            for h in top_list[:10]:
                n = as_pct(h.get("pct") or h.get("percent"))
                if n is not None:
                    s += n
            if s:
                top10_pct = s
    if top10_pct is None:
        top10_pct = parse_top10_from_text(source_text)
    if top10_pct is not None:
        embed.add_field(name="Top 10 Holders", value=f"{top10_pct:.1f}%", inline=True)

    creator = find_creator(ca, pump, rug)
    dev_pct = None
    if creator and total_supply:
        dev_pct = get_wallet_token_pct(creator, ca, total_supply)
    if dev_pct is None and creator and holders and total_supply:
        matched = next((h for h in holders if holder_owner(h) == creator), None)
        if matched is not None:
            dev_pct = (holder_amount(matched) / total_supply) * 100
    if dev_pct is None:
        dev_pct = 0.0

    embed.add_field(name="Dev Holding", value=f"{dev_pct:.2f}%", inline=True)
    return embed


def bubble_url(ca: str) -> str:
    return f"https://v2.bubblemaps.io/map?address={ca}&chain=solana"


def with_bubblemaps_link(text: str, ca: str) -> str:
    link = f"[Bubblemaps]({bubble_url(ca)})"
    raw = text or ""
    if re.search(r"\[Bubblemaps", raw, re.I):
        return raw

    patched = re.sub(
        r"(\[DexTools\]\([^)]+\))",
        rf"\1 · {link}",
        raw,
        count=1,
        flags=re.I,
    )
    if patched != raw:
        return patched

    patched = re.sub(
        r"(\[DexScreener\]\([^)]+\))",
        rf"\1 · {link}",
        raw,
        count=1,
        flags=re.I,
    )
    if patched != raw:
        return patched

    return (raw.rstrip() + f" · {link}").strip()


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
    extra = build_enrichment_embed(ca, source_text)

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
        await message.reply(embed=extra, mention_author=False)
    except Exception as e:
        print(f"[replace error] {e}")
        await message.reply(embed=extra, mention_author=False)


client.run(DISCORD_BOT_TOKEN)

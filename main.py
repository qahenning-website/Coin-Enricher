"""
Coin Enricher Bot
One message: original text + Bubblemaps + Extra Coin Info
with 🟢 / 🟡 / 🔴 flags. No "safe call" wording.
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

COLOR = {"green": 0x3BA55D, "yellow": 0xFEE75C, "red": 0xED4245}

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


def pause():
    time.sleep(1.2)


def get_dex_pairs(ca: str):
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{ca}",
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("pairs") or []
    except Exception as e:
        print(f"[dexscreener error] {ca}: {e}")
        return []


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
    """Only accept a real 0-100 percent. Drop garbage like 309%."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    if 0 < n <= 1:
        n *= 100
    if n > 100:
        return None
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
        try:
            held += float(tok.get("uiAmount") or 0)
        except (TypeError, ValueError):
            pass
    if not supply or supply <= 0:
        return 0.0 if held == 0 else None
    return (held / supply) * 100


def find_creator(pump, rug):
    if pump:
        creator = pump.get("creator") or pump.get("creatorAddress")
        if creator:
            return creator
    if rug:
        creator = rug.get("creator") or (rug.get("token") or {}).get("creator")
        if creator:
            return creator
    return None


def money(n):
    if n is None:
        return None
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}k"
    return f"${n:.0f}"


def authority_on(value) -> bool:
    if value is None or value == "" or value is False:
        return False
    if isinstance(value, str) and value.lower() in ("none", "null", "0"):
        return False
    return True


def lp_status(rug) -> str | None:
    if not rug:
        return None
    risks = rug.get("risks") or []
    names = " ".join((r.get("name") or "") + " " + (r.get("description") or "") for r in risks).lower()
    if "lp unlocked" in names or "unlocked liquidity" in names:
        return "unlocked"
    if "lp burned" in names or "burned" in names:
        return "burned"
    markets = rug.get("markets") or []
    locked_pcts = []
    burned = False
    for m in markets:
        lp = m.get("lp") or m
        pct = lp.get("lpLockedPct") or lp.get("lockedPct")
        if pct is not None:
            try:
                locked_pcts.append(float(pct))
            except (TypeError, ValueError):
                pass
        if lp.get("lpBurned") or lp.get("burned"):
            burned = True
    if burned:
        return "burned"
    if locked_pcts:
        best = max(locked_pcts)
        if best >= 95:
            return "locked"
        if best >= 50:
            return "partial"
        return "unlocked"
    if rug.get("lpLocked") is True:
        return "locked"
    if rug.get("lpLocked") is False:
        return "unlocked"
    return None


def mark_top10(pct):
    if pct is None:
        return None, "unknown     ⚪"
    if pct >= 50:
        return "red", f"{pct:.1f}%     🔴 whales own a lot"
    if pct >= 30:
        return "yellow", f"{pct:.1f}%     🟡 kinda concentrated"
    return "green", f"{pct:.1f}%     🟢 spread out"


def mark_dev(pct):
    if pct is None:
        return None, "unknown     ⚪"
    if pct <= 0.05:
        return "red", f"{pct:.2f}%     🔴 already sold"
    if pct < 1.5:
        return "yellow", f"{pct:.2f}%     🟡 small bag"
    return "green", f"{pct:.2f}%     🟢 still holding"


def mark_liq(liq, mcap):
    if liq is None:
        return None, "unknown     ⚪"
    label = money(liq)
    if mcap and mcap > 0:
        ratio = mcap / liq if liq else 999
        if liq < 3000 or ratio >= 80:
            return "red", f"{label}     🔴 tiny vs mcap"
        if liq < 10000 or ratio >= 25:
            return "yellow", f"{label}     🟡 thin"
    if liq < 3000:
        return "red", f"{label}     🔴 tiny"
    if liq < 10000:
        return "yellow", f"{label}     🟡 thin"
    return "green", f"{label}     🟢 ok vs mcap"


def mark_holders(n):
    if n is None:
        return None, "unknown     ⚪"
    if n < 40:
        return "red", f"{n}     🔴 almost nobody in"
    if n < 150:
        return "yellow", f"{n}     🟡 small crowd"
    return "green", f"{n}     🟢 not thin"


def worst_color(colors: list[str]) -> str:
    if "red" in colors:
        return "red"
    if "yellow" in colors:
        return "yellow"
    if "green" in colors:
        return "green"
    return "yellow"


def build_enrichment_embed(ca: str, source_text: str = ""):
    overview = get_token_overview(ca)
    pause()
    holders = get_holders(ca)
    if not holders:
        pause()
        holders = get_holders(ca)

    pairs = get_dex_pairs(ca)
    pump = get_pump_coin(ca)
    rug = get_rugcheck(ca)

    unix_time = None
    if pump and pump.get("created_timestamp"):
        ts = pump["created_timestamp"]
        unix_time = int(ts / 1000) if ts > 10_000_000_000 else int(ts)
    if not unix_time and pairs:
        times = [p.get("pairCreatedAt") for p in pairs if p.get("pairCreatedAt")]
        if times:
            ms = min(times)
            unix_time = int(ms / 1000) if ms > 10_000_000_000 else int(ms)

    total_supply = None
    holder_count = None
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
        holder_count = overview.get("holder") or overview.get("holders")
        try:
            holder_count = int(holder_count) if holder_count is not None else None
        except (TypeError, ValueError):
            holder_count = None
    if not total_supply:
        total_supply = get_onchain_supply(ca)

    liq = None
    mcap = None
    if pairs:
        liqs = [p.get("liquidity", {}).get("usd") for p in pairs if isinstance(p.get("liquidity"), dict)]
        liqs = [float(x) for x in liqs if x is not None]
        if liqs:
            liq = max(liqs)
        caps = [p.get("marketCap") or p.get("fdv") for p in pairs]
        caps = [float(x) for x in caps if x is not None]
        if caps:
            mcap = max(caps)

    top10_pct = None
    top1_pct = None

    # Prefer Rugcheck percents. Birdeye raw amount / supply often explodes past 100%.
    if rug:
        top_list = rug.get("topHolders") or []
        if top_list:
            parts = [as_pct(h.get("pct") or h.get("percent")) for h in top_list[:10]]
            parts = [p for p in parts if p is not None]
            if parts:
                summed = sum(parts)
                top10_pct = as_pct(summed) or (100.0 if summed > 100 else None)
                top1_pct = parts[0]
    if top10_pct is None:
        top10_pct = parse_top10_from_text(source_text)
    if top10_pct is None and holders:
        parts = []
        for h in holders[:10]:
            n = as_pct(h.get("percentage") or h.get("percent") or h.get("ui_percentage"))
            if n is not None:
                parts.append(n)
        if parts:
            summed = sum(parts)
            top10_pct = as_pct(summed) or (100.0 if summed > 100 else None)
            top1_pct = top1_pct or parts[0]
    if top10_pct is None and holders and total_supply and total_supply > 0:
        guessed = (sum(holder_amount(h) for h in holders[:10]) / total_supply) * 100
        top10_pct = as_pct(guessed)
        if holders and top1_pct is None:
            top1_pct = as_pct((holder_amount(holders[0]) / total_supply) * 100)

    creator = find_creator(pump, rug)
    dev_pct = None
    if creator and total_supply:
        dev_pct = get_wallet_token_pct(creator, ca, total_supply)
    if dev_pct is None and creator and holders and total_supply:
        matched = next((h for h in holders if holder_owner(h) == creator), None)
        if matched is not None:
            dev_pct = (holder_amount(matched) / total_supply) * 100
    token = (rug or {}).get("token") or {}
    mint_on = authority_on(token.get("mintAuthority")) if rug else None
    freeze_on = authority_on(token.get("freezeAuthority")) if rug else None
    lp = lp_status(rug)

    migrated = None
    if pump:
        if pump.get("complete") or pump.get("raydium_pool") or pump.get("migrated"):
            migrated = True
        elif str(ca).endswith("pump"):
            migrated = False

    colors = []
    t10_c, t10_v = mark_top10(top10_pct)
    dv_c, dv_v = mark_dev(dev_pct)
    lq_c, lq_v = mark_liq(liq, mcap)
    hd_c, hd_v = mark_holders(holder_count)
    for c in (t10_c, dv_c, lq_c, hd_c):
        if c:
            colors.append(c)

    flag_lines = []
    if mint_on is True:
        colors.append("red")
        flag_lines.append("🔴 Mint ON  — they can print more coins")
    elif mint_on is False:
        colors.append("green")
        flag_lines.append("🟢 Mint off")
    else:
        flag_lines.append("⚪ Mint unknown")

    if freeze_on is True:
        colors.append("red")
        flag_lines.append("🔴 Freeze ON  — they can lock wallets")
    elif freeze_on is False:
        colors.append("green")
        flag_lines.append("🟢 Freeze off")
    else:
        flag_lines.append("⚪ Freeze unknown")

    if lp == "unlocked":
        colors.append("red")
        flag_lines.append("🔴 LP unlocked  — they can pull the pool")
    elif lp == "partial":
        colors.append("yellow")
        flag_lines.append("🟡 LP partly locked")
    elif lp in ("burned", "locked"):
        colors.append("green")
        flag_lines.append(f"🟢 LP {lp}")
    else:
        flag_lines.append("⚪ LP unknown")

    if migrated is False:
        colors.append("yellow")
        flag_lines.append("🟡 Still on Pump")
    elif migrated is True:
        colors.append("green")
        flag_lines.append("🟢 Migrated")
    else:
        flag_lines.append("⚪ Curve unknown")

    if top1_pct is not None and top1_pct >= 20:
        colors.append("red")
        flag_lines.append(f"🔴 Top wallet {top1_pct:.1f}%")
    elif top1_pct is not None and top1_pct >= 10:
        colors.append("yellow")
        flag_lines.append(f"🟡 Top wallet {top1_pct:.1f}%")
    elif top1_pct is not None:
        flag_lines.append(f"🟢 Top wallet {top1_pct:.1f}%")
    else:
        flag_lines.append("⚪ Top wallet unknown")

    tone = worst_color(colors)
    embed = discord.Embed(title="Extra Coin Info", color=COLOR[tone])

    if unix_time:
        clock, age_str = format_eastern(int(unix_time))
        embed.add_field(name="Launched", value=f"{clock} ({age_str})", inline=False)
    else:
        embed.add_field(name="Launched", value="unknown     ⚪", inline=False)

    embed.add_field(name="Top 10", value=t10_v, inline=False)
    embed.add_field(name="Dev", value=dv_v, inline=False)
    embed.add_field(name="Liq", value=lq_v, inline=False)
    embed.add_field(name="Holders", value=hd_v, inline=False)
    embed.add_field(name="Flags", value="\n".join(flag_lines), inline=False)
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

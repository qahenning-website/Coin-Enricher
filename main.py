"""
Coin Enricher Bot
One Discord message: original text + Bubblemaps + Extra Coin Info.
Top 10 / top wallet from Solana getTokenLargestAccounts (multi-RPC).
Unknown if a field cannot be fetched. No "safe call" text.
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
RPC_URLS = [
    url
    for url in (
        os.environ.get("SOLANA_RPC_URL", "").strip(),
        "https://1rpc.io/solana",
        "https://solana.drpc.org",
        "https://api.mainnet-beta.solana.com",
    )
    if url
]

BIRDEYE_HEADERS = {
    "X-API-KEY": BIRDEYE_API_KEY,
    "x-chain": "solana",
}
COLOR = {"green": 0x3BA55D, "yellow": 0xFEE75C, "red": 0xED4245}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def extract_ca(text: str):
    found = SOL_CA_REGEX.findall(text or "")
    for c in found:
        if any(ch.isdigit() for ch in c):
            return c
    return found[0] if found else None


def pause():
    time.sleep(1.2)


def birdeye_get(url, params, label, ca):
    try:
        r = requests.get(url, headers=BIRDEYE_HEADERS, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("data")
    except Exception as e:
        print(f"[{label} error] {ca}: {e}")
        return None


def rpc(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_err = None
    for url in RPC_URLS:
        try:
            r = requests.post(url, json=payload, timeout=12)
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                last_err = data["error"]
                print(f"[rpc {method} {url}] {data['error']}")
                continue
            return data.get("result")
        except Exception as e:
            last_err = e
            print(f"[rpc {method} {url}] {e}")
    print(f"[rpc {method} failed] {last_err}")
    return None


def get_token_overview(ca):
    return birdeye_get(
        "https://public-api.birdeye.so/defi/token_overview",
        {"address": ca},
        "overview",
        ca,
    )


def get_holders(ca, limit=20):
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


def get_dex_pairs(ca):
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


def get_pump_coin(ca):
    for url in (
        f"https://frontend-api.pump.fun/coins/{ca}",
        f"https://frontend-api-v3.pump.fun/coins/{ca}",
    ):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200 and isinstance(r.json(), dict):
                return r.json()
        except Exception as e:
            print(f"[pump error] {ca}: {e}")
    return None


def get_rugcheck(ca):
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{ca}/report", timeout=12)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[rugcheck error] {ca}: {e}")
    return None


def as_pct(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n < 0 or n > 100:
        return None
    if 0 < n <= 1:
        n *= 100
        if n > 100:
            return None
    return n


def parse_top10_from_text(text):
    if not text:
        return None
    m = re.search(r"top\s*10[^0-9%]{0,16}(\d+(?:\.\d+)?)\s*%", text, re.I)
    return as_pct(m.group(1)) if m else None


def format_eastern(unix_seconds):
    launched = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    launched_et = launched.astimezone(EASTERN)
    hours = (datetime.now(timezone.utc) - launched).total_seconds() / 3600
    age = f"{hours:.1f}h ago" if hours < 48 else f"{hours / 24:.1f}d ago"
    return launched_et.strftime("%Y-%m-%d %I:%M %p") + " EST", age


def token_supply(ca):
    result = rpc("getTokenSupply", [ca])
    if not isinstance(result, dict):
        return None, None
    val = result.get("value") or {}
    decimals = val.get("decimals")
    try:
        decimals = int(decimals) if decimals is not None else None
    except (TypeError, ValueError):
        decimals = None
    ui = val.get("uiAmount")
    try:
        if ui is not None:
            return float(ui), decimals
    except (TypeError, ValueError):
        pass
    try:
        raw = float(val.get("amount") or 0)
        if decimals is not None:
            return raw / (10 ** decimals), decimals
    except (TypeError, ValueError):
        pass
    return None, decimals


def account_ui_amount(acc, decimals):
    if acc.get("uiAmount") is not None:
        try:
            return float(acc["uiAmount"])
        except (TypeError, ValueError):
            pass
    try:
        raw = float(acc.get("amount") or 0)
        if decimals is not None:
            return raw / (10 ** decimals)
        return raw
    except (TypeError, ValueError):
        return 0.0


def largest_holder_pcts(ca, supply, decimals):
    result = rpc("getTokenLargestAccounts", [ca])
    if not isinstance(result, dict):
        return None, None
    rows = result.get("value") or []
    if not rows or not supply or supply <= 0:
        return None, None
    amounts = [account_ui_amount(a, decimals) for a in rows]
    amounts = [a for a in amounts if a >= 0]
    if not amounts:
        return None, None
    top1 = as_pct((amounts[0] / supply) * 100)
    raw10 = (sum(amounts[:10]) / supply) * 100
    top10 = as_pct(raw10)
    if top10 is None and raw10 > 100:
        top10 = 100.0
    return top10, top1


def wallet_token_pct(wallet, mint, supply, decimals):
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
        held += account_ui_amount(tok, decimals)
    if not supply or supply <= 0:
        return 0.0 if held == 0 else None
    raw = (held / supply) * 100
    return as_pct(raw) if raw <= 100 else 100.0


def find_creator(pump, rug):
    if pump:
        c = pump.get("creator") or pump.get("creatorAddress")
        if c:
            return c
    if rug:
        c = rug.get("creator") or (rug.get("token") or {}).get("creator")
        if c:
            return c
    return None


def money(n):
    if n is None:
        return None
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}k"
    return f"${n:.0f}"


def authority_on(value):
    if value is None or value == "" or value is False:
        return False
    if isinstance(value, str) and value.lower() in ("none", "null", "0"):
        return False
    return True


def lp_status(rug):
    if not rug:
        return None
    blob = " ".join(
        (r.get("name") or "") + " " + (r.get("description") or "")
        for r in (rug.get("risks") or [])
    ).lower()
    if "lp unlocked" in blob or "unlocked liquidity" in blob:
        return "unlocked"
    if "lp burned" in blob:
        return "burned"
    locked = []
    burned = False
    for m in rug.get("markets") or []:
        lp = m.get("lp") or m
        pct = lp.get("lpLockedPct") or lp.get("lockedPct")
        if pct is not None:
            try:
                locked.append(float(pct))
            except (TypeError, ValueError):
                pass
        if lp.get("lpBurned") or lp.get("burned"):
            burned = True
    if burned:
        return "burned"
    if locked:
        best = max(locked)
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
    if mcap and mcap > 0 and liq:
        ratio = mcap / liq
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


def worst_color(colors):
    if "red" in colors:
        return "red"
    if "yellow" in colors:
        return "yellow"
    if "green" in colors:
        return "green"
    return "yellow"


def build_enrichment_embed(ca, source_text=""):
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
        created = [p.get("pairCreatedAt") for p in pairs if p.get("pairCreatedAt")]
        if created:
            ms = min(created)
            unix_time = int(ms / 1000) if ms > 10_000_000_000 else int(ms)

    supply, decimals = token_supply(ca)
    holder_count = None
    if overview:
        if supply is None:
            raw = (
                overview.get("supply")
                or overview.get("totalSupply")
                or overview.get("circulatingSupply")
            )
            try:
                supply = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                supply = None
        holder_count = overview.get("holder") or overview.get("holders")
        try:
            holder_count = int(holder_count) if holder_count is not None else None
        except (TypeError, ValueError):
            holder_count = None

    liq = mcap = None
    if pairs:
        liqs = [
            p.get("liquidity", {}).get("usd")
            for p in pairs
            if isinstance(p.get("liquidity"), dict)
        ]
        liqs = [float(x) for x in liqs if x is not None]
        if liqs:
            liq = max(liqs)
        caps = [p.get("marketCap") or p.get("fdv") for p in pairs]
        caps = [float(x) for x in caps if x is not None]
        if caps:
            mcap = max(caps)

    top10_pct, top1_pct = largest_holder_pcts(ca, supply, decimals)

    if top10_pct is None and rug:
        parts = [
            as_pct(h.get("pct") or h.get("percent"))
            for h in (rug.get("topHolders") or [])[:10]
        ]
        parts = [p for p in parts if p is not None]
        if parts:
            s = sum(parts)
            top10_pct = as_pct(s) or (100.0 if s > 100 else None)
            top1_pct = top1_pct or parts[0]
    if top10_pct is None:
        top10_pct = parse_top10_from_text(source_text)

    creator = find_creator(pump, rug)
    dev_pct = None
    if creator and supply:
        dev_pct = wallet_token_pct(creator, ca, supply, decimals)

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
    if migrated is None and pairs:
        dexes = {(p.get("dexId") or "").lower() for p in pairs}
        if dexes & {"pumpswap", "raydium", "meteora"}:
            migrated = True

    colors = []
    t10_c, t10_v = mark_top10(top10_pct)
    dv_c, dv_v = mark_dev(dev_pct)
    lq_c, lq_v = mark_liq(liq, mcap)
    hd_c, hd_v = mark_holders(holder_count)
    for c in (t10_c, dv_c, lq_c, hd_c):
        if c:
            colors.append(c)

    flags = []
    if mint_on is True:
        colors.append("red")
        flags.append("🔴 Mint ON  — they can print more coins")
    elif mint_on is False:
        colors.append("green")
        flags.append("🟢 Mint off")
    else:
        flags.append("⚪ Mint unknown")

    if freeze_on is True:
        colors.append("red")
        flags.append("🔴 Freeze ON  — they can lock wallets")
    elif freeze_on is False:
        colors.append("green")
        flags.append("🟢 Freeze off")
    else:
        flags.append("⚪ Freeze unknown")

    if lp == "unlocked":
        colors.append("red")
        flags.append("🔴 LP unlocked  — they can pull the pool")
    elif lp == "partial":
        colors.append("yellow")
        flags.append("🟡 LP partly locked")
    elif lp in ("burned", "locked"):
        colors.append("green")
        flags.append(f"🟢 LP {lp}")
    else:
        flags.append("⚪ LP unknown")

    if migrated is False:
        colors.append("yellow")
        flags.append("🟡 Still on Pump")
    elif migrated is True:
        colors.append("green")
        flags.append("🟢 Migrated")
    else:
        flags.append("⚪ Curve unknown")

    if top1_pct is not None and top1_pct >= 20:
        colors.append("red")
        flags.append(f"🔴 Top wallet {top1_pct:.1f}%")
    elif top1_pct is not None and top1_pct >= 10:
        colors.append("yellow")
        flags.append(f"🟡 Top wallet {top1_pct:.1f}%")
    elif top1_pct is not None:
        flags.append(f"🟢 Top wallet {top1_pct:.1f}%")
    else:
        flags.append("⚪ Top wallet unknown")

    embed = discord.Embed(title="Extra Coin Info", color=COLOR[worst_color(colors)])
    if unix_time:
        clock, age = format_eastern(int(unix_time))
        embed.add_field(name="Launched", value=f"{clock} ({age})", inline=False)
    else:
        embed.add_field(name="Launched", value="unknown     ⚪", inline=False)
    embed.add_field(name="Top 10", value=t10_v, inline=False)
    embed.add_field(name="Dev", value=dv_v, inline=False)
    embed.add_field(name="Liq", value=lq_v, inline=False)
    embed.add_field(name="Holders", value=hd_v, inline=False)
    embed.add_field(name="Flags", value="\n".join(flags), inline=False)
    return embed


def bubble_url(ca):
    return f"https://v2.bubblemaps.io/map?address={ca}&chain=solana"


def with_bubblemaps_link(text, ca):
    link = f"[Bubblemaps]({bubble_url(ca)})"
    raw = text or ""
    if re.search(r"\[Bubblemaps", raw, re.I):
        return raw
    for label in ("DexTools", "DexScreener"):
        patched = re.sub(
            rf"(\[{label}\]\([^)]+\))",
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

    source = message.content or ""
    for e in message.embeds:
        source += " " + (e.title or "") + " " + (e.description or "")
        for f in e.fields:
            source += " " + (f.name or "") + " " + (f.value or "")

    ca = extract_ca(source) or extract_ca(message.content or "")
    if not ca:
        return

    await asyncio.sleep(1)
    extra = build_enrichment_embed(ca, source)
    combined = with_bubblemaps_link(message.content or "", ca)
    embeds = []
    for e in message.embeds:
        try:
            embeds.append(discord.Embed.from_dict(e.to_dict()))
        except Exception:
            pass
    embeds = (embeds + [extra])[:10]

    try:
        await message.delete()
        await message.channel.send(content=combined or None, embeds=embeds)
    except discord.Forbidden:
        await message.reply(embed=extra, mention_author=False)
    except Exception as e:
        print(f"[replace error] {e}")
        await message.reply(embed=extra, mention_author=False)


client.run(DISCORD_BOT_TOKEN)

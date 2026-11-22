"""Discord bot that replies to Facebook Marketplace links with a rich embed.

Run:  DISCORD_TOKEN=... python bot.py
"""

import asyncio
import html
import json
import logging
import os
import re
import time
from html.parser import HTMLParser
from typing import Optional

import aiohttp
import discord

log = logging.getLogger("fb-marketplace-bot")

MAX_LINKS_PER_MESSAGE = 3
MAX_GALLERY_IMAGES = 4  # Discord renders up to 4 same-url embeds as one gallery
CACHE_TTL = 15 * 60
FETCH_TIMEOUT = 10
MAX_BODY_BYTES = 3_000_000
EMBED_COLOR = 0x1877F2

# Facebook's own link-preview crawler UA is the most likely to be served Open Graph
# tags instead of a login wall; Discordbot is the fallback.
USER_AGENTS = (
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
)

_HOST = r"(?:[a-z0-9-]+\.)?facebook\.com"
LINK_RE = re.compile(
    rf"https?://{_HOST}/(?:"
    r"marketplace/item/\d+"  # canonical
    r"|marketplace/[^/\s<>]+/item/\d+"  # location-scoped variant
    r"|share/(?:[a-z]/)?[A-Za-z0-9_-]+"  # share links (resolved via redirect)
    r")[^\s<>]*",
    re.IGNORECASE,
)
ITEM_ID_RE = re.compile(r"/marketplace/(?:[^/]+/)?item/(\d+)")


def extract_links(text: str) -> list[str]:
    """Return unique candidate Facebook Marketplace/share URLs in order of appearance."""
    seen, out = set(), []
    for m in LINK_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:!?)]}>'\"")
        key = (ITEM_ID_RE.search(url) or [None, url.lower()])[1]
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out[:MAX_LINKS_PER_MESSAGE]


class _MetaParser(HTMLParser):
    """Collects <meta property/name=... content=...> pairs (repeats kept in order)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = dict(attrs)
        key = a.get("property") or a.get("name")
        if key and a.get("content"):
            self.meta.setdefault(key.lower(), []).append(a["content"])


def _json_str(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw.replace("\\/", "/")


def parse_listing(page: str) -> dict:
    """Extract listing info from Facebook HTML. Best effort; every field is optional."""
    parser = _MetaParser()
    parser.feed(page)
    meta = parser.meta

    def first(*keys):
        for k in keys:
            if meta.get(k):
                return html.unescape(meta[k][0]).strip()
        return None

    info = {
        "title": first("og:title", "twitter:title"),
        "description": first("og:description", "description", "twitter:description"),
        "url": first("og:url"),
        "price": None,
        "location": None,
        "images": [],
    }

    # Images: og:image tags first, then photos embedded in the page's JSON blobs.
    images = [html.unescape(u) for u in meta.get("og:image", [])]
    for m in re.finditer(
        r'"(?:listing_photos|all_listing_photos)"\s*:\s*\[(.*?)\]\s*[,}]', page, re.DOTALL
    ):
        for u in re.findall(r'"(?:uri|image_uri)"\s*:\s*"(https:[^"]+)"', m.group(1)):
            images.append(_json_str(u))
    seen = set()
    for u in images:
        if u.startswith("http") and u not in seen:
            seen.add(u)
            info["images"].append(u)

    # Price / location live in embedded JSON when Facebook includes it.
    m = re.search(r'"listing_price"\s*:\s*\{[^{}]*?"formatted_amount"\s*:\s*"([^"]+)"', page)
    if m:
        info["price"] = _json_str(m.group(1))
    m = re.search(
        r'"(?:location_text|location)"\s*:\s*\{[^{}]*?"text"\s*:\s*"([^"]+)"', page
    )
    if m:
        info["location"] = _json_str(m.group(1))
    if not info["title"]:
        m = re.search(r'"marketplace_listing_title"\s*:\s*"([^"]+)"', page)
        if m:
            info["title"] = _json_str(m.group(1))
    return info


def is_useful(info: dict) -> bool:
    """False for login walls / generic Facebook pages with no listing content."""
    if not info.get("images") and not info.get("description"):
        return False
    title = (info.get("title") or "").strip().lower()
    return title not in {"facebook", "log in to facebook", "log into facebook", "error"}


def build_embeds(url: str, info: dict) -> list[discord.Embed]:
    """First embed carries the text; extra same-url embeds make Discord show an image gallery."""
    title = info.get("title") or "Facebook Marketplace listing"
    embed = discord.Embed(
        title=title[:256],
        url=url,
        description=(info.get("description") or "")[:1500] or None,
        color=EMBED_COLOR,
    )
    if info.get("price"):
        embed.add_field(name="Price", value=info["price"][:1024], inline=True)
    if info.get("location"):
        embed.add_field(name="Location", value=info["location"][:1024], inline=True)
    embed.set_footer(text="Facebook Marketplace")

    images = info.get("images", [])[:MAX_GALLERY_IMAGES]
    if images:
        embed.set_image(url=images[0])
    embeds = [embed]
    for img in images[1:]:
        extra = discord.Embed(url=url)
        extra.set_image(url=img)
        embeds.append(extra)
    extra_count = len(info.get("images", [])) - len(images)
    if extra_count > 0:
        embed.set_footer(text=f"Facebook Marketplace • +{extra_count} more photo(s) on the listing")
    return embeds


class MarketplaceFetcher:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.cache: dict[str, tuple[float, Optional[tuple[str, dict]]]] = {}

    async def _get(self, url: str, ua: str) -> Optional[tuple[str, str]]:
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        async with self.session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = await resp.content.read(MAX_BODY_BYTES)
            return str(resp.url), body.decode(resp.get_encoding() or "utf-8", "replace")

    async def fetch(self, url: str) -> Optional[tuple[str, dict]]:
        """Return (canonical_url, info) for a Marketplace listing, or None."""
        cached = self.cache.get(url)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1]

        result = None
        for ua in USER_AGENTS:
            try:
                got = await self._get(url, ua)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("fetch failed for %s (%s): %s", url, ua[:20], exc)
                continue
            if not got:
                continue
            final_url, page = got
            info = parse_listing(page)
            canonical = info.get("url") or final_url
            # Share links must resolve to a marketplace item; skip other FB content.
            item = ITEM_ID_RE.search(final_url) or ITEM_ID_RE.search(canonical)
            if "/login" in final_url or not item or not is_useful(info):
                continue
            canonical = f"https://www.facebook.com/marketplace/item/{item.group(1)}/"
            result = (canonical, info)
            break

        self.cache[url] = (time.time(), result)
        if len(self.cache) > 500:
            self.cache.pop(next(iter(self.cache)))
        return result


class MarketplaceBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable in the Developer Portal
        super().__init__(intents=intents)
        self.fetcher: Optional[MarketplaceFetcher] = None

    async def setup_hook(self):
        self.fetcher = MarketplaceFetcher(aiohttp.ClientSession())

    async def close(self):
        if self.fetcher:
            await self.fetcher.session.close()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        links = extract_links(message.content)
        if not links:
            return
        async with message.channel.typing():
            results = await asyncio.gather(*(self.fetcher.fetch(u) for u in links))
        failed = False
        for result in results:
            if not result:
                failed = True
                continue
            url, info = result
            try:
                await message.reply(embeds=build_embeds(url, info), mention_author=False)
            except discord.HTTPException as exc:
                log.warning("could not send embed: %s", exc)
        if failed:
            try:  # unobtrusive signal: Facebook gave us nothing to preview
                await message.add_reaction("⚠️")
            except discord.HTTPException:
                pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable.")
    MarketplaceBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()

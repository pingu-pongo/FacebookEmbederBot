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
# www pages only carry the seller's description. Price and location are only exposed in
# the og:description Facebook serves to non-Facebook crawlers on the m. host:
# "<title> - $2200, in Waterford, Michigan. Browse similar Auto Parts or message the seller..."
LITE_UA = USER_AGENTS[1]
LITE_URL = "https://m.facebook.com/marketplace/item/{}/"
LITE_DESC_RE = re.compile(
    r"^(?P<head>.+?)(?:,\s+in\s+(?P<loc>.+?))?\.\s+Browse similar\s+(?P<cat>.+?)"
    r"\s+or message the seller",
    re.DOTALL,
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
    """Collects <meta property/name=... content=...> pairs (repeats kept in order) and <title>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.title:
            self._in_title = True
        if tag != "meta":
            return
        a = dict(attrs)
        key = a.get("property") or a.get("name")
        if key and a.get("content"):
            self.meta.setdefault(key.lower(), []).append(a["content"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _json_str(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw.replace("\\/", "/")


def _looks_like_price(text: str) -> bool:
    return bool(re.search(r"\d", text)) or text.strip().lower() == "free"


def parse_lite_description(desc: str, title: Optional[str]) -> Optional[dict]:
    """Split the m.-host og:description into price/location/category, or None if it isn't one."""
    m = LITE_DESC_RE.match(desc or "")
    if not m:
        return None
    head = m.group("head").strip()
    price = None
    if title and head.startswith(f"{title} - "):
        price = head[len(title) + 3 :].strip()
    elif " - " in head:
        price = head.rsplit(" - ", 1)[1].strip()
    if price and not _looks_like_price(price):
        price = None
    return {
        "price": price,
        "location": (m.group("loc") or "").strip() or None,
        "category": m.group("cat").strip() or None,
    }


def _title_tag_details(tag: str, title: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'<title> - <category> - <city> | Facebook Marketplace' -> (category, city)."""
    tag = html.unescape(tag).strip()
    tag = re.sub(r"\s*\|\s*Facebook Marketplace\s*$", "", tag)
    if not title or not tag.startswith(f"{title} - "):
        return None, None
    parts = tag[len(title) + 3 :].rsplit(" - ", 1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (None, parts[0].strip())


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
        "category": None,
        "images": [],
    }

    lite = parse_lite_description(info["description"], info["title"])
    if lite:
        info.update(lite)
        info["description"] = None  # synthetic summary, not the seller's text

    # Images: og:image tags first, then photos embedded in the page's JSON blobs.
    images = [html.unescape(u) for u in meta.get("og:image", [])]
    for m in re.finditer(
        r'"(?:listing_photos|all_listing_photos)"\s*:\s*\[(.*?)\]\s*[,}]', page, re.DOTALL
    ):
        for u in re.findall(r'"(?:uri|image_uri)"\s*:\s*"(https:[^"]+)"', m.group(1)):
            images.append(_json_str(u))
    seen = set()
    for u in images:
        # Same photo is listed at several sizes; key on the path without the query string.
        key = u.split("?", 1)[0]
        if u.startswith("http") and key not in seen:
            seen.add(key)
            info["images"].append(u)
    if len(info["images"]) > 1:  # generic crawler copy of the main photo
        info["images"] = [u for u in info["images"] if "lookaside.fbsbx.com" not in u] or info["images"]

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
    # <title> carries "<category> - <city>" on the www page even when nothing else does.
    if not (info["location"] and info["category"]):
        category, city = _title_tag_details(parser.title, info["title"])
        info["category"] = info["category"] or category
        info["location"] = info["location"] or city
    return info


def is_useful(info: dict) -> bool:
    """False for login walls / generic Facebook pages with no listing content."""
    if not info.get("images") and not info.get("description"):
        return False
    title = (info.get("title") or "").strip().lower()
    return title not in {
        "facebook",
        "log in to facebook",
        "log into facebook",
        "error",
        "facebook marketplace: buy and sell items locally or shipped",
    }


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
    if info.get("category"):
        embed.add_field(name="Category", value=info["category"][:1024], inline=True)
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
            # StreamReader.read(n) returns a single chunk, so loop until the cap.
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_BODY_BYTES:
                    break
            body = b"".join(chunks)[:MAX_BODY_BYTES]
            return str(resp.url), body.decode(resp.get_encoding() or "utf-8", "replace")

    async def _load(self, url: str, ua: str) -> Optional[tuple[str, dict]]:
        """Fetch one page and return (canonical_url, info) if it is a real listing."""
        try:
            got = await self._get(url, ua)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("fetch failed for %s (%s): %s", url, ua[:20], exc)
            return None
        if not got:
            return None
        final_url, page = got
        info = parse_listing(page)
        canonical = info.get("url") or final_url
        # Share links must resolve to a marketplace item; skip other FB content.
        item = ITEM_ID_RE.search(final_url) or ITEM_ID_RE.search(canonical)
        if "/login" in final_url or not item or not is_useful(info):
            return None
        return f"https://www.facebook.com/marketplace/item/{item.group(1)}/", info

    async def _primary(self, url: str) -> Optional[tuple[str, dict]]:
        for ua in USER_AGENTS:
            result = await self._load(url, ua)
            if result:
                return result
        return None

    async def fetch(self, url: str) -> Optional[tuple[str, dict]]:
        """Return (canonical_url, info) for a Marketplace listing, or None."""
        cached = self.cache.get(url)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1]

        item = ITEM_ID_RE.search(url)
        if item:
            canonical_url = f"https://www.facebook.com/marketplace/item/{item.group(1)}/"
            primary, lite = await asyncio.gather(
                self._primary(canonical_url), self._load(LITE_URL.format(item.group(1)), LITE_UA)
            )
        else:  # share link: need the redirect target before we know the item id
            primary = await self._primary(url)
            lite = None
            if primary:
                item_id = ITEM_ID_RE.search(primary[0]).group(1)
                lite = await self._load(LITE_URL.format(item_id), LITE_UA)

        result = primary or lite
        if primary and lite:
            for key in ("price", "location", "category"):
                if lite[1].get(key):
                    primary[1][key] = lite[1][key]
            if not primary[1]["images"]:
                primary[1]["images"] = lite[1]["images"]

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

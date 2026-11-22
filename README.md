# Facebook Marketplace Embed Bot

A single-file Discord bot (`bot.py`) that watches chat for Facebook Marketplace links and replies with an embed of the listing's preview image and description. It does nothing else.

## Setup

1. Create an application + bot at <https://discord.com/developers/applications>.
2. On the **Bot** tab, enable the **Message Content Intent** (required to read links).
3. Invite the bot with the `bot` scope and permissions: View Channels, Send Messages, Embed Links, Read Message History, Add Reactions.
4. Run it:

```powershell
pip install -r requirements.txt
$env:DISCORD_TOKEN = "your-token"
python bot.py
```

(`export DISCORD_TOKEN=...` on Linux/macOS.) The token is only ever read from the environment.

## Docker / Unraid

```bash
docker build -t marketplace-embed-bot .
docker run -d --name marketplace-embed-bot --restart unless-stopped -e DISCORD_TOKEN=your-token marketplace-embed-bot
```

On Unraid, open a terminal on the server, clone this repo, run the two commands above, or add a container in the Docker tab using the built image with a `DISCORD_TOKEN` variable and "Restart: unless-stopped". No ports need to be exposed.

## What it detects

`facebook.com/marketplace/item/<id>` on any subdomain (`www`, `m`, `web`, `mbasic`…), location-scoped `marketplace/<city>/item/<id>` links, and `facebook.com/share/...` links (followed through redirects; ignored unless they land on a Marketplace item). Up to 3 unique listings per message; bots are ignored.

## What you get

- Title, description, price, location, category and the main image (plus extra photos as a gallery when Facebook exposes them).
- **Media:** Discord allows one image per embed, but several embeds sharing the same URL render as a gallery. The bot shows up to **4 photos** this way; if the listing has more, the footer says how many were left out and the title links to the listing. Video and a true "all media" view are not possible in embeds.

## Limitations

- Facebook has no public Marketplace API. The bot reads Open Graph tags from the public page using link-preview crawler user agents. Facebook may serve a login wall, rate-limit server IPs, or strip details at any time; extra photos are best-effort and the HTML structure can change.
- Price: the normal `www` page does not contain the price without login. It only appears in the Open Graph description Facebook serves to non-Facebook crawlers on the `m.facebook.com` host (`<title> - $2200, in City, State. Browse similar <Category> ...`), so the bot fetches that page too and parses price, location and category from it. If Facebook changes that text, the embed still works but shows no price.
- If no preview can be fetched (login wall, removed/private listing), the bot reacts with ⚠️ instead of replying.
- Image URLs are signed CDN links and may eventually expire in old messages.
- Results are cached in memory for 15 minutes.
- Only verified offline (link parsing, HTML parsing, embed building, fetcher against a mock server); not tested against live listings or a live Discord server.

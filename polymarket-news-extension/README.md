# Polymarket Live News — Chrome Extension

A Chrome extension that detects which prediction market you're viewing on Polymarket and surfaces live news, Reddit posts, and Telegram channel messages relevant to that market — all in a clean popup.

## Features

| Source | Works without config | Notes |
|---|---|---|
| **Google News RSS** | ✅ Yes | Searches news.google.com RSS — no API key needed |
| **NewsAPI.org** | ⚡ Optional | Free tier: 100 req/day. Better article coverage |
| **Reddit** | ✅ Yes | Searches reddit.com public JSON API |
| **Telegram** | 🔑 Bot token needed | Reads messages from channels your bot is a member of |

## Installation

### 1. Load the extension in Chrome

1. Clone / download this folder
2. Open Chrome → `chrome://extensions`
3. Enable **Developer mode** (top right toggle)
4. Click **Load unpacked** → select the `polymarket-news-extension` folder
5. Pin the extension to your toolbar

### 2. Configure (optional but recommended)

Click the **⚙** gear icon in the popup (or right-click the extension → *Options*) to open Settings:

#### NewsAPI (free)
1. Register at [newsapi.org/register](https://newsapi.org/register)
2. Copy your API key → paste into **NewsAPI.org API Key**

#### Reddit subreddits
Enter specific subreddits to focus results, e.g.:
```
politics, worldnews, economics, geopolitics, PredictionMarkets
```
Leave blank to search all of Reddit.

#### Telegram
1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow steps → copy the token
2. Add your bot as a **member or admin** to the channels you want to monitor
3. In Settings, paste the token and list the channel usernames (without `@`), e.g.:
   ```
   polymarketalerts, cryptonewsflash, worldpoliticsnews
   ```
4. Click **Test Telegram Connection** to verify

## How it works

```
Content Script (content.js)
  └── Detects current Polymarket market from DOM/meta tags
  └── Extracts title, description, search keywords
  └── Notifies background service worker

Background (background.js)
  └── Fetches Google News RSS    ─┐
  └── Fetches NewsAPI.org        ─┤── merged & deduplicated
  └── Fetches Reddit JSON API    ─┤
  └── Fetches Telegram Bot API   ─┘
  └── Caches results (3 min TTL)
  └── Auto-refreshes on alarm (configurable interval)

Popup (popup.html/js)
  └── Shows current market
  └── Displays feed in 3 tabs: News | Reddit | Telegram
  └── Manual refresh button
```

## File Structure

```
polymarket-news-extension/
├── manifest.json       Chrome extension manifest (v3)
├── content.js          Runs on polymarket.com — extracts market info
├── background.js       Service worker — fetches news from all sources
├── popup.html/css/js   Extension popup UI
├── options.html/css/js Settings page
└── icons/
    ├── icon.svg        Source SVG icon
    ├── icon16.png
    ├── icon48.png
    └── icon128.png
```

## Permissions Used

| Permission | Why |
|---|---|
| `storage` | Saves your API keys and settings |
| `activeTab` | Reads the current tab URL/title |
| `alarms` | Periodic background refresh |
| Host permissions | Fetch from news/Reddit/Telegram APIs |

## Privacy

- API keys are stored locally in `chrome.storage.sync` (synced to your Chrome profile, never sent to any server we control)
- All network requests go directly from your browser to the respective APIs (Google, Reddit, Telegram, NewsAPI)
- No analytics, no tracking

## Troubleshooting

**No market detected**: Make sure you're on a full event page like `polymarket.com/event/...`, not the homepage.

**No news showing**: The extension uses Google News RSS by default. If results are empty, the market is very niche — try adding a NewsAPI key for broader coverage.

**Reddit empty**: Reddit's API rate-limits aggressive requests. Wait a minute and try again.

**Telegram not working**: 
- Verify the bot token with the "Test" button in Settings
- Make sure the bot is added to the channel (try sending a message in the channel and checking `getUpdates`)
- Telegram Bot API's `getUpdates` only returns messages received *after* the bot joined

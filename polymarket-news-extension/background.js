// Polymarket Live News - Background Service Worker
// Fetches news from Google News RSS, Reddit, and Telegram

const CACHE_TTL = 3 * 60 * 1000; // 3 minutes
const cache = new Map(); // key -> { data, ts }

// ─── Helpers ──────────────────────────────────────────────────────────────────

function cacheGet(key) {
  const entry = cache.get(key);
  if (!entry) return null;
  if (Date.now() - entry.ts > CACHE_TTL) {
    cache.delete(key);
    return null;
  }
  return entry.data;
}

function cacheSet(key, data) {
  cache.set(key, { data, ts: Date.now() });
}

async function fetchJSON(url, headers = {}) {
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${url}`);
  return res.json();
}

async function fetchText(url, headers = {}) {
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${url}`);
  return res.text();
}

function encodeQuery(q) {
  return encodeURIComponent(q.trim());
}

// ─── Google News RSS (no API key needed) ─────────────────────────────────────

async function fetchGoogleNews(query) {
  const key = `gnews:${query}`;
  const cached = cacheGet(key);
  if (cached) return cached;

  const url = `https://news.google.com/rss/search?q=${encodeQuery(query)}&hl=en-US&gl=US&ceid=US:en`;
  const xml = await fetchText(url);

  const parser = new DOMParser();
  const doc = parser.parseFromString(xml, "text/xml");
  const items = [...doc.querySelectorAll("item")].slice(0, 10);

  const results = items.map((item) => ({
    source: "google_news",
    title: item.querySelector("title")?.textContent?.trim() ?? "",
    url: item.querySelector("link")?.textContent?.trim() ??
      item.querySelector("guid")?.textContent?.trim() ?? "#",
    publishedAt: item.querySelector("pubDate")?.textContent?.trim() ?? "",
    snippet: item.querySelector("description")?.textContent?.replace(/<[^>]+>/g, "").trim() ?? "",
    sourceName: item.querySelector("source")?.textContent?.trim() ?? "Google News",
  }));

  cacheSet(key, results);
  return results;
}

// ─── NewsAPI.org ──────────────────────────────────────────────────────────────

async function fetchNewsAPI(query, apiKey) {
  if (!apiKey) return [];
  const key = `newsapi:${query}`;
  const cached = cacheGet(key);
  if (cached) return cached;

  const url = `https://newsapi.org/v2/everything?q=${encodeQuery(query)}&sortBy=publishedAt&pageSize=10&language=en&apiKey=${apiKey}`;
  const data = await fetchJSON(url);

  const results = (data.articles ?? []).map((a) => ({
    source: "newsapi",
    title: a.title ?? "",
    url: a.url ?? "#",
    publishedAt: a.publishedAt ?? "",
    snippet: a.description ?? "",
    sourceName: a.source?.name ?? "NewsAPI",
    imageUrl: a.urlToImage ?? null,
  }));

  cacheSet(key, results);
  return results;
}

// ─── Reddit ───────────────────────────────────────────────────────────────────

async function fetchReddit(query, subreddits = []) {
  const key = `reddit:${query}:${subreddits.join(",")}`;
  const cached = cacheGet(key);
  if (cached) return cached;

  let url;
  if (subreddits.length > 0) {
    const sr = subreddits.map((s) => s.replace(/^r\//, "")).join("+");
    url = `https://www.reddit.com/r/${sr}/search.json?q=${encodeQuery(query)}&sort=new&restrict_sr=1&limit=10`;
  } else {
    url = `https://www.reddit.com/search.json?q=${encodeQuery(query)}&sort=new&limit=10`;
  }

  const data = await fetchJSON(url, { "User-Agent": "PolymarketNewsExt/1.0" });
  const posts = data?.data?.children ?? [];

  const results = posts.map((p) => {
    const d = p.data;
    return {
      source: "reddit",
      title: d.title ?? "",
      url: `https://www.reddit.com${d.permalink}`,
      publishedAt: new Date(d.created_utc * 1000).toISOString(),
      snippet: d.selftext
        ? d.selftext.slice(0, 200) + (d.selftext.length > 200 ? "…" : "")
        : "",
      sourceName: `r/${d.subreddit}`,
      score: d.score,
      numComments: d.num_comments,
      thumbnail: d.thumbnail?.startsWith("http") ? d.thumbnail : null,
    };
  });

  cacheSet(key, results);
  return results;
}

// ─── Telegram Bot API ─────────────────────────────────────────────────────────
// Requires: bot token + channels where the bot is a member.
// We call getUpdates or use channel history via getChatHistory (Bot API v5+).

async function fetchTelegram(query, botToken, channels = []) {
  if (!botToken || channels.length === 0) return [];

  const results = [];
  const queryLower = query.toLowerCase();

  for (const channel of channels) {
    const chatId = channel.startsWith("@") ? channel : `@${channel}`;
    const key = `telegram:${chatId}`;
    let messages = cacheGet(key);

    if (!messages) {
      try {
        // getUpdates doesn't support reading channel history directly,
        // but forwardMessages from channels works if bot is admin.
        // We use the sendMessage trick: fetch recent channel messages via
        // the unofficial but widely-used getChatHistory workaround.
        // Official approach: use webhooks or getUpdates (bot must be in channel).
        // Here we use getUpdates to grab any buffered messages.
        const url = `https://api.telegram.org/bot${botToken}/getUpdates?limit=100&allowed_updates=["channel_post"]`;
        const data = await fetchJSON(url);
        messages = (data.result ?? [])
          .filter((u) => u.channel_post?.chat?.username === channel.replace(/^@/, ""))
          .map((u) => ({
            source: "telegram",
            title: u.channel_post.text?.split("\n")[0]?.slice(0, 120) ?? "(media)",
            url: u.channel_post.chat?.username
              ? `https://t.me/${u.channel_post.chat.username}/${u.channel_post.message_id}`
              : "#",
            publishedAt: new Date(u.channel_post.date * 1000).toISOString(),
            snippet: u.channel_post.text?.slice(0, 300) ?? "",
            sourceName: `@${u.channel_post.chat?.username ?? channel}`,
          }));
        cacheSet(key, messages);
      } catch (err) {
        console.warn(`Telegram fetch failed for ${chatId}:`, err);
        messages = [];
      }
    }

    // Filter by query keywords
    const keywords = queryLower.split(/\s+/).filter((w) => w.length > 2);
    const relevant = messages.filter((m) => {
      const text = (m.title + " " + m.snippet).toLowerCase();
      return keywords.some((kw) => text.includes(kw));
    });

    results.push(...relevant);
  }

  return results;
}

// ─── Aggregate all sources ────────────────────────────────────────────────────

async function fetchAllSources(marketInfo, settings) {
  const query = marketInfo.searchQuery || marketInfo.title || "";
  if (!query) return { news: [], reddit: [], telegram: [] };

  const [news, newsApiResults, reddit, telegram] = await Promise.allSettled([
    fetchGoogleNews(query),
    fetchNewsAPI(query, settings.newsApiKey),
    fetchReddit(query, settings.subreddits ?? []),
    fetchTelegram(query, settings.telegramBotToken, settings.telegramChannels ?? []),
  ]);

  const googleNews = news.status === "fulfilled" ? news.value : [];
  const naNews = newsApiResults.status === "fulfilled" ? newsApiResults.value : [];

  // Merge and deduplicate news sources
  const allNews = [...googleNews, ...naNews];
  const seenTitles = new Set();
  const deduped = allNews.filter((item) => {
    const key = item.title.toLowerCase().slice(0, 60);
    if (seenTitles.has(key)) return false;
    seenTitles.add(key);
    return true;
  });

  return {
    news: deduped,
    reddit: reddit.status === "fulfilled" ? reddit.value : [],
    telegram: telegram.status === "fulfilled" ? telegram.value : [],
    query,
    fetchedAt: Date.now(),
    errors: {
      news: news.status === "rejected" ? news.reason?.message : null,
      newsApi: newsApiResults.status === "rejected" ? newsApiResults.reason?.message : null,
      reddit: reddit.status === "rejected" ? reddit.reason?.message : null,
      telegram: telegram.status === "rejected" ? telegram.reason?.message : null,
    },
  };
}

// ─── State ────────────────────────────────────────────────────────────────────

let currentMarket = null;
let currentFeed = null;
let refreshAlarmName = "polymarket-news-refresh";

async function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(
      {
        newsApiKey: "",
        telegramBotToken: "",
        telegramChannels: [],
        subreddits: [],
        refreshInterval: 5, // minutes
      },
      resolve
    );
  });
}

async function doRefresh() {
  if (!currentMarket) return;
  const settings = await getSettings();
  try {
    currentFeed = await fetchAllSources(currentMarket, settings);
    // Notify any open popups
    chrome.runtime.sendMessage({ type: "FEED_UPDATED", payload: currentFeed }).catch(() => {});
  } catch (err) {
    console.error("Feed refresh error:", err);
  }
}

// ─── Message Handlers ─────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "MARKET_DETECTED") {
    const info = message.payload;
    if (!info.title && !info.slug) return;

    const changed =
      !currentMarket ||
      currentMarket.title !== info.title ||
      currentMarket.url !== info.url;

    currentMarket = info;

    if (changed) {
      currentFeed = null; // invalidate cache on market change
      doRefresh();
    }
    return;
  }

  if (message.type === "GET_FEED") {
    if (currentFeed) {
      sendResponse({ payload: currentFeed });
    } else {
      // Trigger a fresh fetch, then respond
      doRefresh().then(() => {
        sendResponse({ payload: currentFeed });
      });
      return true; // async response
    }
    return;
  }

  if (message.type === "GET_MARKET") {
    sendResponse({ payload: currentMarket });
    return;
  }

  if (message.type === "FORCE_REFRESH") {
    currentFeed = null;
    doRefresh().then(() => {
      sendResponse({ payload: currentFeed });
    });
    return true;
  }
});

// ─── Periodic Refresh Alarm ───────────────────────────────────────────────────

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === refreshAlarmName) doRefresh();
});

async function setupAlarm() {
  const settings = await getSettings();
  const intervalMinutes = Math.max(1, settings.refreshInterval ?? 5);
  chrome.alarms.create(refreshAlarmName, { periodInMinutes: intervalMinutes });
}

setupAlarm();

// Re-setup alarm when settings change
chrome.storage.onChanged.addListener((changes) => {
  if (changes.refreshInterval) {
    chrome.alarms.clear(refreshAlarmName, () => setupAlarm());
  }
});

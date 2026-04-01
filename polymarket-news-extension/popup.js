// Polymarket Live News — Popup Script

"use strict";

// ─── DOM refs ──────────────────────────────────────────────────────────────

const $statusBadge  = document.getElementById("status-badge");
const $marketCard   = document.getElementById("market-card");
const $noMarket     = document.getElementById("no-market");
const $marketTitle  = document.getElementById("market-title");
const $marketQuery  = document.getElementById("market-query");
const $fetchedAt    = document.getElementById("fetched-at");
const $tabs         = document.getElementById("tabs");
const $feedCont     = document.getElementById("feed-container");
const $loading      = document.getElementById("loading");
const $btnRefresh   = document.getElementById("btn-refresh");
const $newsCount    = document.getElementById("news-count");
const $redditCount  = document.getElementById("reddit-count");
const $telegramCount = document.getElementById("telegram-count");

const panels = {
  news:     document.getElementById("panel-news"),
  reddit:   document.getElementById("panel-reddit"),
  telegram: document.getElementById("panel-telegram"),
};

let activeTab = "news";

// ─── Helpers ───────────────────────────────────────────────────────────────

function relativeTime(isoOrRfc) {
  try {
    const d = new Date(isoOrRfc);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60)   return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  } catch { return ""; }
}

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function setStatus(label, cls) {
  $statusBadge.textContent = label;
  $statusBadge.className = `badge badge--${cls}`;
}

// ─── Rendering ────────────────────────────────────────────────────────────

function renderNewsItem(item) {
  const sourceClass = item.source === "newsapi" ? "news" : "news";
  return `
    <a class="feed-item" href="${esc(item.url)}" target="_blank" rel="noopener">
      <div class="feed-item__meta">
        <span class="feed-item__source feed-item__source--${sourceClass}">${esc(item.sourceName)}</span>
        <span>·</span>
        <span>${esc(relativeTime(item.publishedAt))}</span>
      </div>
      <div class="feed-item__title">${esc(item.title)}</div>
      ${item.snippet ? `<div class="feed-item__snippet">${esc(item.snippet)}</div>` : ""}
    </a>
  `;
}

function renderRedditItem(item) {
  return `
    <a class="feed-item" href="${esc(item.url)}" target="_blank" rel="noopener">
      <div class="feed-item__meta">
        <span class="feed-item__source feed-item__source--reddit">${esc(item.sourceName)}</span>
        <span>·</span>
        <span>${esc(relativeTime(item.publishedAt))}</span>
      </div>
      <div class="feed-item__title">${esc(item.title)}</div>
      ${item.snippet ? `<div class="feed-item__snippet">${esc(item.snippet)}</div>` : ""}
      <div class="feed-item__stats">
        <span>▲ ${item.score ?? 0}</span>
        <span>💬 ${item.numComments ?? 0}</span>
      </div>
    </a>
  `;
}

function renderTelegramItem(item) {
  return `
    <a class="feed-item" href="${esc(item.url)}" target="_blank" rel="noopener">
      <div class="feed-item__meta">
        <span class="feed-item__source feed-item__source--telegram">${esc(item.sourceName)}</span>
        <span>·</span>
        <span>${esc(relativeTime(item.publishedAt))}</span>
      </div>
      <div class="feed-item__title">${esc(item.title)}</div>
      ${item.snippet ? `<div class="feed-item__snippet">${esc(item.snippet)}</div>` : ""}
    </a>
  `;
}

function emptyPanel(icon, message) {
  return `
    <div class="panel-empty">
      <div class="panel-empty-icon">${icon}</div>
      <p>${esc(message)}</p>
    </div>
  `;
}

function renderFeed(feed) {
  if (!feed) return;

  // News
  if (feed.errors?.news) {
    panels.news.innerHTML = `<div class="error-banner">⚠ ${esc(feed.errors.news)}</div>` +
      emptyPanel("📰", "Could not load news.");
  } else if (feed.news.length === 0) {
    panels.news.innerHTML = emptyPanel("📰", "No news found for this market yet.");
  } else {
    panels.news.innerHTML = feed.news.map(renderNewsItem).join("");
  }
  $newsCount.textContent = feed.news.length || "";

  // Reddit
  if (feed.errors?.reddit) {
    panels.reddit.innerHTML = `<div class="error-banner">⚠ ${esc(feed.errors.reddit)}</div>` +
      emptyPanel("🤖", "Could not load Reddit posts.");
  } else if (feed.reddit.length === 0) {
    panels.reddit.innerHTML = emptyPanel("🤖", "No Reddit posts found. Try adding subreddits in Settings.");
  } else {
    panels.reddit.innerHTML = feed.reddit.map(renderRedditItem).join("");
  }
  $redditCount.textContent = feed.reddit.length || "";

  // Telegram
  if (!feed.telegram || feed.telegram.length === 0) {
    panels.telegram.innerHTML = emptyPanel(
      "✈",
      feed.errors?.telegram
        ? `Error: ${feed.errors.telegram}`
        : "No Telegram messages. Add your bot token and channels in Settings."
    );
  } else {
    panels.telegram.innerHTML = feed.telegram.map(renderTelegramItem).join("");
  }
  $telegramCount.textContent = (feed.telegram ?? []).length || "";

  // Fetched-at timestamp
  if (feed.fetchedAt) {
    $fetchedAt.textContent = `Updated ${relativeTime(new Date(feed.fetchedAt).toISOString())}`;
  }
}

// ─── Tab switching ────────────────────────────────────────────────────────

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    activeTab = tab;

    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");

    Object.entries(panels).forEach(([key, panel]) => {
      panel.classList.toggle("active", key === tab);
    });
  });
});

// ─── Show market + feed ───────────────────────────────────────────────────

function showMarket(market) {
  if (!market || (!market.title && !market.slug)) {
    $marketCard.classList.add("hidden");
    $noMarket.classList.remove("hidden");
    $tabs.classList.add("hidden");
    $feedCont.classList.add("hidden");
    setStatus("Idle", "idle");
    return;
  }

  $noMarket.classList.add("hidden");
  $marketCard.classList.remove("hidden");
  $tabs.classList.remove("hidden");
  $feedCont.classList.remove("hidden");

  $marketTitle.textContent = market.title || market.slug || "Unknown Market";
  $marketQuery.textContent = market.searchQuery || market.title || "";
}

function setLoading(on) {
  $loading.classList.toggle("hidden", !on);
  if (on) setStatus("Loading", "loading");
}

// ─── Init ────────────────────────────────────────────────────────────────

async function init() {
  setLoading(true);

  // Try to get the active tab's market info directly from content script
  let market = null;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.url?.includes("polymarket.com")) {
      const response = await chrome.tabs.sendMessage(tab.id, { type: "GET_MARKET_INFO" });
      market = response?.payload ?? null;
      if (market?.title) {
        // Also notify background so it can refresh
        chrome.runtime.sendMessage({ type: "MARKET_DETECTED", payload: market });
      }
    }
  } catch (_) {
    // Content script not ready — fall back to background
  }

  if (!market?.title) {
    const bgResp = await new Promise((res) =>
      chrome.runtime.sendMessage({ type: "GET_MARKET" }, res)
    );
    market = bgResp?.payload ?? null;
  }

  showMarket(market);

  if (!market?.title) {
    setLoading(false);
    setStatus("Idle", "idle");
    return;
  }

  // Get feed from background
  const feedResp = await new Promise((res) =>
    chrome.runtime.sendMessage({ type: "GET_FEED" }, res)
  );
  const feed = feedResp?.payload ?? null;

  setLoading(false);
  if (feed) {
    renderFeed(feed);
    setStatus("Live", "live");
  } else {
    setStatus("Error", "error");
  }
}

// ─── Refresh button ───────────────────────────────────────────────────────

$btnRefresh.addEventListener("click", async () => {
  $btnRefresh.disabled = true;
  setLoading(true);
  Object.values(panels).forEach((p) => (p.innerHTML = ""));
  $newsCount.textContent = "";
  $redditCount.textContent = "";
  $telegramCount.textContent = "";

  const resp = await new Promise((res) =>
    chrome.runtime.sendMessage({ type: "FORCE_REFRESH" }, res)
  );
  const feed = resp?.payload ?? null;

  setLoading(false);
  $btnRefresh.disabled = false;

  if (feed) {
    renderFeed(feed);
    setStatus("Live", "live");
  } else {
    setStatus("Error", "error");
  }
});

// ─── Live updates from background ────────────────────────────────────────

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "FEED_UPDATED" && message.payload) {
    renderFeed(message.payload);
    setStatus("Live", "live");
  }
});

// ─── Go ───────────────────────────────────────────────────────────────────

init();

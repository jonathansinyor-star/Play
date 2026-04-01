// Polymarket Live News - Content Script
// Extracts market info from the current Polymarket page and notifies the extension

(function () {
  "use strict";

  let lastMarketInfo = null;
  let observerActive = false;

  function extractMarketInfo() {
    const info = {
      url: window.location.href,
      title: null,
      question: null,
      category: null,
      tags: [],
      slug: null,
    };

    // Extract slug from URL
    // URLs: /event/slug or /market/slug
    const urlMatch = window.location.pathname.match(
      /\/(event|market)\/([^/?#]+)/
    );
    if (urlMatch) {
      info.slug = urlMatch[2]
        .replace(/-/g, " ")
        .replace(/\b\w/g, (c) => c.toUpperCase());
    }

    // Try OG / meta tags first — most reliable
    const ogTitle = document.querySelector('meta[property="og:title"]');
    if (ogTitle && ogTitle.content) {
      info.title = ogTitle.content.replace(" | Polymarket", "").trim();
    }

    const ogDescription = document.querySelector(
      'meta[property="og:description"]'
    );
    if (ogDescription && ogDescription.content) {
      info.question = ogDescription.content.trim();
    }

    // Fallback: page <h1> (market question heading)
    if (!info.title) {
      const h1 = document.querySelector("h1");
      if (h1) info.title = h1.innerText.trim();
    }

    // Try to grab category / tags from breadcrumbs or tag chips
    const tagEls = document.querySelectorAll(
      '[class*="tag"], [class*="Tag"], [class*="category"], [class*="Category"]'
    );
    tagEls.forEach((el) => {
      const text = el.innerText.trim();
      if (text && text.length < 40 && !info.tags.includes(text)) {
        info.tags.push(text);
      }
    });

    // Build a concise search query from title + tags
    if (info.title) {
      const stopWords = new Set([
        "will", "the", "a", "an", "be", "in", "on", "at", "to", "for",
        "of", "and", "or", "is", "by", "with", "from", "that", "this",
        "what", "who", "how", "when", "does", "did", "has", "have",
        "are", "was", "were",
      ]);
      const words = info.title
        .split(/\s+/)
        .filter((w) => w.length > 2 && !stopWords.has(w.toLowerCase()))
        .slice(0, 6);
      info.searchQuery = words.join(" ");
    }

    info.timestamp = Date.now();
    return info;
  }

  function notifyExtension(info) {
    if (!info.title && !info.slug) return;

    const infoStr = JSON.stringify(info);
    const lastStr = JSON.stringify(lastMarketInfo);

    if (infoStr === lastStr) return; // no change

    lastMarketInfo = info;
    chrome.runtime.sendMessage({ type: "MARKET_DETECTED", payload: info });
  }

  function run() {
    const info = extractMarketInfo();
    notifyExtension(info);
  }

  // Initial extraction (page may already be loaded)
  run();

  // Watch for SPA navigation changes (Polymarket is a React SPA)
  if (!observerActive) {
    observerActive = true;

    // Listen for pushState / replaceState
    const originalPushState = history.pushState.bind(history);
    const originalReplaceState = history.replaceState.bind(history);

    history.pushState = function (...args) {
      originalPushState(...args);
      setTimeout(run, 800);
    };
    history.replaceState = function (...args) {
      originalReplaceState(...args);
      setTimeout(run, 800);
    };

    window.addEventListener("popstate", () => setTimeout(run, 800));

    // MutationObserver for DOM updates (re-render)
    const observer = new MutationObserver(() => {
      const info = extractMarketInfo();
      notifyExtension(info);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // Re-run on page visibility change (switching tabs back)
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") run();
  });

  // Listen for requests from popup
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "GET_MARKET_INFO") {
      sendResponse({ payload: extractMarketInfo() });
    }
  });
})();

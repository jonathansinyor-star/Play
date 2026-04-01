// Polymarket Live News — Options Page

"use strict";

const form         = document.getElementById("settings-form");
const saveBanner   = document.getElementById("save-banner");
const btnClear     = document.getElementById("btn-clear");
const btnTestTg    = document.getElementById("btn-test-telegram");
const tgTestResult = document.getElementById("telegram-test-result");

// ─── Load saved settings ──────────────────────────────────────────────────

chrome.storage.sync.get(
  {
    newsApiKey: "",
    telegramBotToken: "",
    telegramChannels: [],
    subreddits: [],
    refreshInterval: 5,
  },
  (settings) => {
    document.getElementById("newsApiKey").value = settings.newsApiKey;
    document.getElementById("telegramBotToken").value = settings.telegramBotToken;
    document.getElementById("telegramChannels").value = (settings.telegramChannels ?? []).join(", ");
    document.getElementById("subreddits").value = (settings.subreddits ?? []).join(", ");
    document.getElementById("refreshInterval").value = settings.refreshInterval;
  }
);

// ─── Save ─────────────────────────────────────────────────────────────────

form.addEventListener("submit", (e) => {
  e.preventDefault();

  const parseList = (val) =>
    val
      .split(",")
      .map((s) => s.trim().replace(/^[@r\/]+/, ""))
      .filter(Boolean);

  const settings = {
    newsApiKey:        document.getElementById("newsApiKey").value.trim(),
    telegramBotToken:  document.getElementById("telegramBotToken").value.trim(),
    telegramChannels:  parseList(document.getElementById("telegramChannels").value),
    subreddits:        parseList(document.getElementById("subreddits").value),
    refreshInterval:   Math.max(1, parseInt(document.getElementById("refreshInterval").value, 10) || 5),
  };

  chrome.storage.sync.set(settings, () => {
    saveBanner.classList.remove("banner--hidden");
    setTimeout(() => saveBanner.classList.add("banner--hidden"), 3000);
  });
});

// ─── Clear ────────────────────────────────────────────────────────────────

btnClear.addEventListener("click", () => {
  if (!confirm("Clear all saved settings?")) return;
  chrome.storage.sync.clear(() => {
    document.getElementById("newsApiKey").value = "";
    document.getElementById("telegramBotToken").value = "";
    document.getElementById("telegramChannels").value = "";
    document.getElementById("subreddits").value = "";
    document.getElementById("refreshInterval").value = "5";
    saveBanner.textContent = "🗑 Settings cleared.";
    saveBanner.classList.remove("banner--hidden");
    setTimeout(() => saveBanner.classList.add("banner--hidden"), 2500);
  });
});

// ─── Test Telegram ────────────────────────────────────────────────────────

btnTestTg.addEventListener("click", async () => {
  const token = document.getElementById("telegramBotToken").value.trim();
  if (!token) {
    showTgResult("⚠ Enter a bot token first.", "err");
    return;
  }

  showTgResult("Testing…", "");
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/getMe`);
    const data = await res.json();
    if (data.ok) {
      showTgResult(`✅ Connected as @${data.result.username}`, "ok");
    } else {
      showTgResult(`❌ ${data.description}`, "err");
    }
  } catch (err) {
    showTgResult(`❌ ${err.message}`, "err");
  }
});

function showTgResult(msg, cls) {
  tgTestResult.textContent = msg;
  tgTestResult.className = "test-result" + (cls ? ` test-result--${cls}` : "");
}

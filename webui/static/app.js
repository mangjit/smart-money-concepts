(() => {
  "use strict";

  const state = {
    symbol: "BTCUSDT",
    market: "crypto",
    timeframe: "1h",
    signal: null,
    chart: null,
    series: null,
    priceLines: [],
    sessionId: window.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}`,
  };

  const $ = (selector) => document.querySelector(selector);
  const elements = {
    chart: $("#chart"),
    symbol: $("#symbol-input"),
    market: $("#market-select"),
    source: $("#data-source"),
    marketBadge: $("#market-badge"),
    instrumentName: $("#instrument-name"),
    chartTitle: $("#chart-title"),
    updated: $("#last-updated"),
    error: $("#error-banner"),
    analyze: $("#analyze-button"),
    refresh: $("#refresh-button"),
    signalStatus: $("#signal-status"),
    signalAction: $("#signal-action"),
    signalConfidence: $("#signal-confidence"),
    levelEntry: $("#level-entry"),
    levelStop: $("#level-stop"),
    levelTarget: $("#level-target"),
    levelRr: $("#level-rr"),
    signalNotice: $("#signal-notice"),
    metricClose: $("#metric-close"),
    metricAtr: $("#metric-atr"),
    metricEma: $("#metric-ema"),
    metricRsi: $("#metric-rsi"),
    reasoning: $("#reasoning-list"),
    exits: $("#exit-list"),
    sourceChip: $("#source-chip"),
    model: $("#model-select"),
    chatForm: $("#chat-form"),
    chatInput: $("#chat-input"),
    chatLog: $("#chat-log"),
    memoryStatus: $("#memory-status"),
    systemStatus: $("#system-status"),
    statusDot: $(".status-dot"),
  };

  function showError(message) {
    elements.error.textContent = message;
    elements.error.hidden = !message;
  }

  function formatPrice(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const number = Number(value);
    const decimals = Math.abs(number) >= 1000 ? 2 : Math.abs(number) >= 1 ? 5 : 8;
    return number.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: decimals });
  }

  function formatTime(timestamp) {
    return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", month: "short", day: "2-digit" }).format(new Date(timestamp));
  }

  function configureChart() {
    const library = window.LightweightCharts;
    if (!library) {
      elements.chart.textContent = "Chart library unavailable. Market analysis remains available when the API is online.";
      elements.chart.style.display = "grid";
      elements.chart.style.placeItems = "center";
      elements.chart.style.color = "#8d9bae";
      return;
    }
    state.chart = library.createChart(elements.chart, {
      width: elements.chart.clientWidth,
      height: elements.chart.clientHeight,
      layout: { background: { type: "solid", color: "#0e1621" }, textColor: "#8d9bae", fontFamily: "Inter, system-ui, sans-serif" },
      grid: { vertLines: { color: "rgba(39, 54, 73, .55)" }, horzLines: { color: "rgba(39, 54, 73, .55)" } },
      rightPriceScale: { borderColor: "#273649" },
      timeScale: { borderColor: "#273649", timeVisible: true, secondsVisible: false },
      crosshair: { vertLine: { color: "rgba(141,155,174,.28)" }, horzLine: { color: "rgba(141,155,174,.28)" } },
    });
    const candleOptions = {
      upColor: "#19c790", downColor: "#ff6475", borderVisible: false,
      wickUpColor: "#19c790", wickDownColor: "#ff6475",
    };
    // v4 and v5 have different series constructors; supporting both keeps the static UI resilient.
    state.series = state.chart.addCandlestickSeries
      ? state.chart.addCandlestickSeries(candleOptions)
      : state.chart.addSeries(library.CandlestickSeries, candleOptions);
    new ResizeObserver(([entry]) => {
      if (state.chart) state.chart.applyOptions({ width: Math.floor(entry.contentRect.width) });
    }).observe(elements.chart);
  }

  function clearPriceLines() {
    if (!state.series) return;
    state.priceLines.forEach((line) => state.series.removePriceLine(line));
    state.priceLines = [];
  }

  function renderChart(candles) {
    if (!state.series) return;
    const data = candles.map((candle) => ({
      time: Math.floor(candle.timestamp / 1000),
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
    }));
    state.series.setData(data);
    state.chart.timeScale().fitContent();
  }

  function addLevel(price, title, color, style = 2) {
    if (!state.series || price === null || price === undefined) return;
    state.priceLines.push(state.series.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
  }

  function updateHeader(response) {
    const isForex = response.market === "forex";
    elements.instrumentName.textContent = response.symbol;
    elements.chartTitle.textContent = `${response.symbol} · ${state.timeframe}`;
    elements.marketBadge.textContent = response.market.toUpperCase();
    elements.marketBadge.className = `pill ${isForex ? "forex" : "crypto"}`;
    elements.source.textContent = response.source;
    elements.updated.textContent = `Last closed bar: ${formatTime(response.candles.at(-1).timestamp)}`;
  }

  function listItems(container, values) {
    container.replaceChildren();
    values.forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      container.append(item);
    });
  }

  function updateSignal(signal) {
    state.signal = signal;
    const actionClass = signal.action === "BUY" ? "buy" : signal.action === "SELL" ? "sell" : "no-signal";
    elements.signalStatus.className = `signal-status ${actionClass}`;
    elements.signalStatus.textContent = signal.action === "NO_SIGNAL" ? "NO SIGNAL" : signal.action;
    elements.signalAction.textContent = signal.action === "NO_SIGNAL" ? "WAIT" : signal.action;
    elements.signalAction.style.color = signal.action === "BUY" ? "var(--green)" : signal.action === "SELL" ? "var(--red)" : "var(--yellow)";
    elements.signalConfidence.textContent = `${signal.confidence}%`;
    elements.levelEntry.textContent = formatPrice(signal.entry_price);
    elements.levelStop.textContent = formatPrice(signal.stop_loss);
    elements.levelTarget.textContent = formatPrice(signal.take_profit);
    elements.levelRr.textContent = signal.risk_reward ? `${signal.risk_reward}:1` : "—";
    elements.signalNotice.textContent = signal.risk_notice;
    elements.metricClose.textContent = formatPrice(signal.technicals.close);
    elements.metricAtr.textContent = formatPrice(signal.technicals.atr);
    elements.metricEma.textContent = `${formatPrice(signal.technicals.ema_fast)} / ${formatPrice(signal.technicals.ema_slow)}`;
    elements.metricRsi.textContent = signal.technicals.rsi.toFixed(1);
    elements.sourceChip.textContent = signal.data_source.includes("Binance") ? "BINANCE" : "YAHOO";
    listItems(elements.reasoning, signal.reasoning);
    listItems(elements.exits, signal.exit_plan);
    clearPriceLines();
    addLevel(signal.entry_price, "Reference entry", "#5d8dff");
    addLevel(signal.stop_loss, "Stop · 1.5 ATR", "#ff6475");
    addLevel(signal.take_profit, "Target · 2R", "#19c790");
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* use status text */ }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  function setBusy(busy) {
    elements.analyze.disabled = busy;
    elements.refresh.disabled = busy;
    elements.analyze.textContent = busy ? "Loading closed candles…" : "Analyze closed candles";
  }

  async function refreshDashboard() {
    state.symbol = elements.symbol.value.trim().toUpperCase().replaceAll("/", "").replaceAll("-", "");
    state.market = elements.market.value;
    if (!state.symbol) return showError("Enter a market symbol before requesting data.");
    elements.symbol.value = state.symbol;
    showError("");
    setBusy(true);
    try {
      const parameters = new URLSearchParams({ symbol: state.symbol, market: state.market, timeframe: state.timeframe, limit: "300" });
      const candleResponse = await requestJson(`/api/market/candles?${parameters}`);
      renderChart(candleResponse.candles);
      updateHeader(candleResponse);
      const signal = await requestJson("/api/signals/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: state.symbol, market: state.market, timeframe: state.timeframe, limit: 300 }),
      });
      updateSignal(signal);
    } catch (error) {
      showError(`Market data or analysis unavailable: ${error.message}`);
      elements.source.textContent = "No verified data loaded";
    } finally {
      setBusy(false);
    }
  }

  function appendChat(role, message) {
    const node = document.createElement("div");
    node.className = `chat-message ${role === "user" ? "user-message" : "assistant-message"}`;
    node.textContent = message;
    elements.chatLog.append(node);
    elements.chatLog.scrollTop = elements.chatLog.scrollHeight;
  }

  async function sendChat(event) {
    event.preventDefault();
    const message = elements.chatInput.value.trim();
    if (!message) return;
    appendChat("user", message);
    elements.chatInput.value = "";
    const button = elements.chatForm.querySelector("button");
    button.disabled = true;
    try {
      const response = await requestJson("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: state.sessionId,
          message,
          model_id: elements.model.value,
          symbol: state.symbol,
          market: state.market,
          timeframe: state.timeframe,
        }),
      });
      appendChat("assistant", response.reply);
      elements.memoryStatus.textContent = response.memory_enabled
        ? "Recent context: MongoDB enabled (30-day server-side retention)."
        : "Recent context: this browser session only (in-memory server fallback).";
    } catch (error) {
      appendChat("assistant", `I could not answer that request: ${error.message}`);
    } finally {
      button.disabled = false;
      elements.chatInput.focus();
    }
  }

  async function loadModelsAndHealth() {
    try {
      const [models, health] = await Promise.all([requestJson("/api/models"), requestJson("/api/health")]);
      elements.model.replaceChildren();
      models.forEach((model) => {
        const option = document.createElement("option");
        option.value = model.id;
        option.textContent = `${model.label}${model.configured ? "" : " · configure server key"}`;
        option.disabled = !model.configured;
        elements.model.append(option);
      });
      elements.systemStatus.textContent = `${health.memory === "mongo" ? "Mongo memory" : "Session memory"} · ${health.telegram_configured ? "Telegram ready" : "Telegram optional"}`;
      elements.statusDot.className = "status-dot connected";
      elements.memoryStatus.textContent = health.memory === "mongo"
        ? "Recent context: MongoDB enabled (30-day server-side retention)."
        : "Recent context: this browser session only (in-memory server fallback).";
    } catch (error) {
      elements.systemStatus.textContent = "API unavailable";
      elements.statusDot.className = "status-dot error";
      showError(`Service health check failed: ${error.message}`);
    }
  }

  function wireInteractions() {
    document.querySelectorAll(".timeframes button").forEach((button) => {
      button.addEventListener("click", () => {
        state.timeframe = button.dataset.timeframe;
        document.querySelectorAll(".timeframes button").forEach((item) => item.classList.toggle("selected", item === button));
        refreshDashboard();
      });
    });
    document.querySelectorAll(".watch-item").forEach((button) => {
      button.addEventListener("click", () => {
        state.symbol = button.dataset.symbol;
        state.market = button.dataset.market;
        elements.symbol.value = state.symbol;
        elements.market.value = state.market;
        document.querySelectorAll(".watch-item").forEach((item) => item.classList.toggle("active", item === button));
        refreshDashboard();
      });
    });
    elements.market.addEventListener("change", () => { state.market = elements.market.value; });
    elements.symbol.addEventListener("keydown", (event) => { if (event.key === "Enter") refreshDashboard(); });
    elements.analyze.addEventListener("click", refreshDashboard);
    elements.refresh.addEventListener("click", refreshDashboard);
    elements.chatForm.addEventListener("submit", sendChat);
  }

  configureChart();
  wireInteractions();
  loadModelsAndHealth();
  refreshDashboard();
})();

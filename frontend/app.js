(function () {
  "use strict";

  var APP_ID = "img-lab";
  var TOOLS = window.ImgLabTools;

  var state = {
    apiBase: "",
    options: null,
    priors: {},
    limits: {},
    speedLimits: {},
    formatNames: {},
    healthVersion: "",
    entries: [],
    stats: { count: 0, bytes: 0 },
    mode: "resolution",
    planValid: false,
    plan: null,
    settings: null,
    generating: false,
    generationJobId: "",
    generationTimer: null,
    speedRunning: false,
    speedJobId: "",
    speedTimer: null,
    previewKey: "",
    previewTimer: null,
    planTimer: null,
    latestSpeed: null,
    toastTimer: null,
    booted: false,
  };

  var elements = {};

  var ELEMENT_IDS = [
    "appVersion",
    "footerVersion",
    "connectionPill",
    "connectionText",
    "themeToggle",
    "tabGenerator",
    "tabSpeed",
    "panelGenerator",
    "panelSpeed",
    "appBanner",
    "appBannerText",
    "appRetryButton",
    "generateForm",
    "modeSegmented",
    "resolutionFields",
    "bytesFields",
    "widthInput",
    "heightInput",
    "targetSize",
    "aspectSelect",
    "formatSelect",
    "formatHint",
    "qualityRange",
    "qualityInput",
    "qualityHint",
    "encoderNote",
    "contentOptions",
    "probeToggle",
    "seedInput",
    "planCard",
    "previewButton",
    "previewImage",
    "previewFallback",
    "planEstimateTag",
    "planDims",
    "planDetail",
    "planNote",
    "planError",
    "generateButton",
    "cancelGenerateButton",
    "batchPanel",
    "batchCount",
    "batchInput",
    "batchError",
    "batchButton",
    "libraryCount",
    "librarySubtitle",
    "exportCsvButton",
    "bundleButton",
    "clearButton",
    "jobBanner",
    "jobTitle",
    "jobDetail",
    "jobProgressFill",
    "jobStage",
    "jobCounter",
    "summaryCount",
    "summaryCountFoot",
    "summaryBytes",
    "summaryConverged",
    "summaryConvergedFoot",
    "summaryLatest",
    "summaryLatestFoot",
    "libraryEmpty",
    "libraryList",
    "libraryFoot",
    "speedForm",
    "speedUrl",
    "speedConcurrency",
    "speedRounds",
    "speedTimeout",
    "speedRequestHint",
    "speedError",
    "speedButton",
    "cancelSpeedButton",
    "speedBadge",
    "speedSubtitle",
    "speedJobBanner",
    "speedJobDetail",
    "speedProgressFill",
    "speedJobStage",
    "speedJobCounter",
    "speedFailure",
    "speedFailureText",
    "speedEmpty",
    "speedSummary",
    "speedP50",
    "speedP50Foot",
    "speedP95",
    "speedP95Foot",
    "speedServerP50",
    "speedServerFoot",
    "speedFailRate",
    "speedFailFoot",
    "timingTotal",
    "timingBar",
    "timingLegend",
    "serverNote",
    "serverCompare",
    "networkBar",
    "networkValue",
    "serverBar",
    "serverValue",
    "compareNote",
    "speedUrlLabel",
    "speedStatuses",
    "speedSamples",
    "previewDialog",
    "previewClose",
    "previewLarge",
    "toast",
  ];

  function byId(id) {
    return document.getElementById(id);
  }

  function cacheElements() {
    ELEMENT_IDS.forEach(function (id) {
      elements[id] = byId(id);
    });
    elements.modeButtons = Array.prototype.slice.call(
      document.querySelectorAll("#modeSegmented .segment")
    );
    elements.tabs = [elements.tabGenerator, elements.tabSpeed];
    elements.panels = {
      generator: elements.panelGenerator,
      speed: elements.panelSpeed,
    };
    elements.contentCards = Array.prototype.slice.call(
      document.querySelectorAll("#contentOptions .content-card")
    );
    elements.timingSegments = Array.prototype.slice.call(
      document.querySelectorAll("#timingBar [data-seg]")
    );
    elements.timingValues = Array.prototype.slice.call(
      document.querySelectorAll("#timingLegend [data-value]")
    );
    elements.generateLabel = elements.generateButton.querySelector(
      "span:not(.button-spinner)"
    );
    elements.speedLabel = elements.speedButton.querySelector(
      "span:not(.button-spinner)"
    );
  }

  // -- small helpers ------------------------------------------------------

  function setText(element, text) {
    if (element) {
      element.textContent = text;
    }
  }

  function clampInt(value, fallback, minimum, maximum) {
    var number = parseInt(value, 10);
    if (!Number.isFinite(number)) {
      number = fallback;
    }
    return Math.max(minimum, Math.min(maximum, number));
  }

  function finiteNumber(value, fallback) {
    var number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function stamp() {
    var date = new Date();
    function pad(value) {
      return value < 10 ? "0" + value : String(value);
    }
    return (
      date.getFullYear() +
      pad(date.getMonth() + 1) +
      pad(date.getDate()) +
      "-" +
      pad(date.getHours()) +
      pad(date.getMinutes()) +
      pad(date.getSeconds())
    );
  }

  function escapeHtml(text) {
    return TOOLS.escapeHtml(text);
  }

  function selectedContent() {
    var checked = document.querySelector('input[name="content"]:checked');
    return checked ? checked.value : "photo";
  }

  function syncContentCards() {
    elements.contentCards.forEach(function (card) {
      var input = card.querySelector("input");
      card.classList.toggle("is-selected", Boolean(input && input.checked));
    });
  }

  // -- api plumbing -------------------------------------------------------

  function ApiError(message, code, kind) {
    this.name = "ApiError";
    this.message = message;
    this.code = code || "";
    this.kind = kind || "api";
  }

  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function pageBasePath() {
    var path = window.location.pathname || "/";
    if (path.charAt(path.length - 1) === "/") {
      return path;
    }
    if (/\/[^/]*\.[^/]*$/.test(path)) {
      return path.replace(/\/[^/]*$/, "/");
    }
    return path + "/";
  }

  function apiCandidates() {
    var candidates = [
      pageBasePath(),
      "/v2/proxy/" + APP_ID + "/",
      "/" + APP_ID + "/",
      "/",
    ];
    var seen = Object.create(null);
    return candidates.filter(function (item) {
      if (seen[item]) {
        return false;
      }
      seen[item] = true;
      return true;
    });
  }

  function setConnection(status) {
    elements.connectionPill.dataset.state = status;
    if (status === "online") {
      setText(elements.connectionText, "服务已连接");
    } else if (status === "offline") {
      setText(elements.connectionText, "服务未连接");
    } else {
      setText(elements.connectionText, "正在连接");
    }
  }

  function updateVersion(version) {
    if (!version) {
      return;
    }
    state.healthVersion = String(version);
    setText(elements.appVersion, "v" + version);
    setText(elements.footerVersion, version);
  }

  function showBanner(message) {
    setText(elements.appBannerText, message || "服务暂时不可用。");
    elements.appBanner.hidden = false;
  }

  function hideBanner() {
    elements.appBanner.hidden = true;
  }

  async function probeBase() {
    var candidates = apiCandidates();
    for (var index = 0; index < candidates.length; index += 1) {
      var base = candidates[index];
      try {
        var response = await fetch(base + "health", {
          method: "GET",
          credentials: "include",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        var type = response.headers.get("content-type") || "";
        if (!response.ok || type.indexOf("application/json") === -1) {
          continue;
        }
        var data = await response.json();
        if (data && data.ok && data.app === APP_ID) {
          state.apiBase = base;
          updateVersion(data.version);
          setConnection("online");
          return data;
        }
      } catch (error) {
        /* Try the next deployment shape. */
      }
    }
    state.apiBase = "";
    setConnection("offline");
    throw new ApiError(
      "无法连接图片实验室服务。请确认应用已启动，然后点击“重新连接”。",
      "unreachable",
      "connection"
    );
  }

  function csrfToken() {
    return TOOLS.getCookieValue(document.cookie, "X-Csrf-Token");
  }

  async function requestBase(base, path, options) {
    var opts = options || {};
    var method = String(opts.method || "GET").toUpperCase();
    var headers = new Headers(opts.headers || {});
    if (!headers.has("Accept")) {
      headers.set("Accept", "application/json");
    }
    var init = {
      method: method,
      credentials: "include",
      cache: "no-store",
      headers: headers,
    };
    if (opts.body !== undefined) {
      init.body =
        typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
      if (!headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
    }
    if (method !== "GET" && method !== "HEAD") {
      var token = csrfToken();
      if (token) {
        headers.set("X-Csrf-Token", token);
      }
    }

    var response;
    try {
      response = await fetch(base + path, init);
    } catch (error) {
      throw new ApiError(
        "网络请求失败，请检查 TOS 连接后重试。",
        "network_error",
        "connection"
      );
    }

    var contentType = response.headers.get("content-type") || "";
    if (contentType.indexOf("application/json") === -1) {
      if (response.status === 401 || response.status === 403 || response.redirected) {
        throw new ApiError(
          "登录状态已失效，请在 TOS 中重新登录后再试。",
          "login_required",
          "login"
        );
      }
      throw new ApiError(
        "服务返回了异常响应（HTTP " + response.status + "）。",
        "bad_response",
        "connection"
      );
    }

    var data;
    try {
      data = await response.json();
    } catch (error) {
      throw new ApiError("服务返回的 JSON 无法解析。", "bad_json", "connection");
    }

    if (!response.ok) {
      throw new ApiError(
        (data && data.error) || "请求失败（HTTP " + response.status + "）。",
        data && data.code,
        "api"
      );
    }
    if (!data || data.ok === false) {
      throw new ApiError(
        (data && data.error) || "服务返回了失败结果。",
        data && data.code,
        "api"
      );
    }
    return data;
  }

  async function request(path, options) {
    if (!state.apiBase) {
      await probeBase();
    }
    try {
      return await requestBase(state.apiBase, path, options);
    } catch (error) {
      if (error && error.kind === "connection") {
        state.apiBase = "";
        setConnection("offline");
      }
      throw error;
    }
  }

  function triggerDownload(blob, filename) {
    var url = URL.createObjectURL(blob);
    var anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1500);
  }

  async function downloadPath(path, filename) {
    if (!state.apiBase) {
      await probeBase();
    }
    var response;
    try {
      response = await fetch(state.apiBase + path, {
        method: "GET",
        credentials: "include",
        cache: "no-store",
        headers: { Accept: "*/*" },
      });
    } catch (error) {
      throw new ApiError(
        "下载失败，请检查 TOS 连接后重试。",
        "network_error",
        "connection"
      );
    }
    if (!response.ok) {
      var message = "下载失败（HTTP " + response.status + "）。";
      var type = response.headers.get("content-type") || "";
      if (type.indexOf("application/json") !== -1) {
        try {
          var data = await response.json();
          if (data && data.error) {
            message = data.error;
          }
        } catch (error) {
          /* Keep the HTTP status message. */
        }
      }
      throw new ApiError(message, "download_failed", "api");
    }
    var blob = await response.blob();
    triggerDownload(blob, filename);
  }

  // -- UI state -----------------------------------------------------------

  function updateThemeLabel() {
    var theme = window.ImgLabTheme;
    if (!theme) {
      return;
    }
    var mode = theme.getMode();
    var label = theme.label(mode);
    elements.themeToggle.setAttribute("aria-label", label + "，点击切换");
    elements.themeToggle.title = "当前：" + label + "，点击切换";
  }

  function setTab(name) {
    var target = name === "speed" ? "speed" : "generator";
    elements.tabs.forEach(function (tab) {
      var selected = tab.dataset.tab === target;
      tab.classList.toggle("is-active", selected);
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.tabIndex = selected ? 0 : -1;
    });
    Object.keys(elements.panels).forEach(function (key) {
      elements.panels[key].hidden = key !== target;
    });
  }

  function updateModeUi() {
    elements.modeButtons.forEach(function (button) {
      var active = button.dataset.mode === state.mode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
    elements.resolutionFields.hidden = state.mode !== "resolution";
    elements.bytesFields.hidden = state.mode !== "bytes";
  }

  function setMode(mode, delay) {
    state.mode = mode === "bytes" ? "bytes" : "resolution";
    updateModeUi();
    schedulePlan(typeof delay === "number" ? delay : 0);
  }

  function updateFormatUi() {
    var format = elements.formatSelect.value;
    var cap =
      state.options && state.options.formats
        ? state.options.formats[format]
        : null;
    var isPng = format === "png";
    elements.qualityRange.disabled = isPng;
    elements.qualityInput.disabled = isPng;
    setText(
      elements.qualityHint,
      isPng
        ? "PNG 为无损编码，质量参数不生效。"
        : "1–100，越大文件越大。"
    );
    if (cap && cap.available === false) {
      setText(
        elements.formatHint,
        cap.detail || "当前系统缺少该编码器，生成时会失败。"
      );
      elements.formatHint.hidden = false;
    } else {
      elements.formatHint.hidden = true;
      setText(elements.formatHint, "");
    }
  }

  function applyOptions(data) {
    state.options = data;
    state.priors = data.priors || {};
    state.limits = data.limits || {};
    state.speedLimits = data.speed || {};
    state.formatNames = data.formatNames || {};

    var caps = data.formats || {};
    Object.keys(caps).forEach(function (format) {
      var option = elements.formatSelect.querySelector(
        'option[value="' + format + '"]'
      );
      if (!option) {
        return;
      }
      var label =
        state.formatNames[format] || TOOLS.formatLabel(format);
      if (caps[format] && caps[format].available === false) {
        option.disabled = true;
        option.textContent = label + "（不可用）";
      } else {
        option.disabled = false;
        option.textContent = label;
      }
    });
    if (elements.formatSelect.selectedOptions[0] &&
        elements.formatSelect.selectedOptions[0].disabled) {
      var fallback = Array.prototype.find.call(
        elements.formatSelect.options,
        function (option) {
          return !option.disabled;
        }
      );
      if (fallback) {
        elements.formatSelect.value = fallback.value;
      }
    }

    var available = Object.keys(caps).filter(function (format) {
      return !caps[format] || caps[format].available !== false;
    });
    setText(
      elements.encoderNote,
      available.length
        ? "本机编码 · " + available.map(TOOLS.formatLabel).join(" / ")
        : "本机编码 · 不可用"
    );
    elements.encoderNote.title = Object.keys(caps)
      .map(function (format) {
        return (
          TOOLS.formatLabel(format) +
          "：" +
          (caps[format].available === false
            ? caps[format].detail || "不可用"
            : "可用")
        );
      })
      .join("\n");

    updateSpeedLimits();
    updateLibraryLimits();
    updateFormatUi();
  }

  function updateSpeedLimits() {
    var limits = state.speedLimits || {};
    var maxConcurrency = clampInt(limits.max_concurrency, 16, 1, 64);
    var maxRounds = clampInt(limits.max_rounds, 20, 1, 100);
    elements.speedConcurrency.max = String(maxConcurrency);
    elements.speedRounds.max = String(maxRounds);
    elements.speedTimeout.max = "60";
    updateSpeedHint();
  }

  function updateLibraryLimits() {
    var maxEntries = clampInt(
      state.options && state.options.library && state.options.library.max_entries,
      200,
      1,
      100000
    );
    var maxBytes =
      (state.options &&
        state.options.library &&
        state.options.library.max_total_bytes) ||
      1610612736;
    setText(
      elements.libraryFoot,
      "最多保留 " +
        maxEntries +
        " 个文件，总计 " +
        TOOLS.formatBytes(maxBytes) +
        "；超出后自动清理最旧的文件。"
    );
    setText(elements.summaryCountFoot, "最多 " + maxEntries + " 个");
  }

  function updateSpeedHint() {
    var concurrency = clampInt(elements.speedConcurrency.value, 4, 1, 16);
    var rounds = clampInt(elements.speedRounds.value, 3, 1, 20);
    var limit = (state.speedLimits && state.speedLimits.max_requests) || 160;
    var total = concurrency * rounds;
    var text =
      "共 " +
      total +
      " 个请求，最多并发 " +
      concurrency +
      " 个。";
    if (total > limit) {
      text += "超过上限 " + limit + "，请减少并发或轮次。";
    }
    setText(elements.speedRequestHint, text);
  }

  // -- plan ---------------------------------------------------------------

  function readSettings() {
    var targetMib = parseFloat(elements.targetSize.value);
    var aspect = parseFloat(elements.aspectSelect.value);
    return {
      mode: state.mode,
      width: Math.round(finiteNumber(elements.widthInput.value, 0)),
      height: Math.round(finiteNumber(elements.heightInput.value, 0)),
      targetBytes: Number.isFinite(targetMib)
        ? Math.round(targetMib * TOOLS.MIB)
        : 0,
      aspect: Number.isFinite(aspect) ? aspect : 4 / 3,
      format: elements.formatSelect.value,
      quality: clampInt(elements.qualityInput.value, 85, 1, 100),
      content: selectedContent(),
      probe: elements.probeToggle.checked,
      seed: clampInt(elements.seedInput.value, 424242, 0, 2147483647),
    };
  }

  function planInput(settings) {
    return {
      mode: settings.mode,
      width: settings.width,
      height: settings.height,
      targetBytes: settings.targetBytes,
      aspect: settings.aspect,
      format: settings.format,
      quality: settings.quality,
      content: settings.content,
      priors: state.priors,
    };
  }

  function schedulePlan(delay) {
    window.clearTimeout(state.planTimer);
    state.planTimer = window.setTimeout(
      updatePlan,
      typeof delay === "number" ? delay : 160
    );
    updateBatchState();
  }

  function setPlanError(message) {
    if (message) {
      setText(elements.planError, message);
      elements.planError.hidden = false;
    } else {
      elements.planError.hidden = true;
      setText(elements.planError, "");
    }
  }

  function clearPreview() {
    elements.previewImage.removeAttribute("src");
    elements.previewButton.classList.remove("has-image");
    setText(elements.previewFallback, "预览");
    state.previewKey = "";
  }

  function renderPlan(settings) {
    var plan = TOOLS.planEstimate(planInput(settings));
    state.plan = plan;
    state.settings = settings;
    state.planValid = Boolean(plan.valid);

    setText(
      elements.planEstimateTag,
      plan.valid
        ? settings.mode === "bytes"
          ? "目标体积"
          : "预估体积"
        : "待修正"
    );

    if (!plan.valid) {
      setText(elements.planDims, "—");
      setText(elements.planDetail, "请修正参数后生成。");
      elements.planNote.hidden = true;
      setPlanError(plan.error);
      clearPreview();
      elements.generateButton.disabled = true;
      return;
    }

    setPlanError("");
    var quality = settings.format === "png" ? "无损" : "q" + plan.quality;
    var detail = [
      TOOLS.formatMp(plan.pixels),
      TOOLS.formatLabel(settings.format) + " " + quality,
      TOOLS.contentLabel(settings.content),
    ];
    if (settings.mode === "bytes") {
      detail.push("目标 " + TOOLS.formatBytes(plan.targetBytes));
      detail.push(TOOLS.aspectLabel(plan.aspect));
    } else {
      detail.push("预估 " + TOOLS.formatBytes(plan.estimateBytes));
    }
    if (settings.probe) {
      detail.push("探针");
    }

    setText(elements.planDims, plan.width + " × " + plan.height);
    setText(elements.planDetail, detail.join(" · "));
    if (plan.note) {
      setText(elements.planNote, plan.note);
      elements.planNote.hidden = false;
    } else {
      elements.planNote.hidden = true;
      setText(elements.planNote, "");
    }

    elements.generateButton.disabled = state.generating || !plan.valid;
    schedulePreview(plan, settings);
  }

  function updatePlan() {
    renderPlan(readSettings());
  }

  function previewUrl(width, height, content, seed, maxSide) {
    var scale = Math.min(1, maxSide / Math.max(1, Math.max(width, height)));
    var previewWidth = Math.max(16, Math.round(width * scale));
    var previewHeight = Math.max(16, Math.round(height * scale));
    return (
      state.apiBase +
      "api/preview?width=" +
      previewWidth +
      "&height=" +
      previewHeight +
      "&content=" +
      encodeURIComponent(content) +
      "&seed=" +
      seed
    );
  }

  function schedulePreview(plan, settings) {
    window.clearTimeout(state.previewTimer);
    if (!plan.valid) {
      return;
    }
    state.previewTimer = window.setTimeout(function () {
      updatePreview(plan, settings);
    }, 280);
  }

  function updatePreview(plan, settings) {
    if (!state.apiBase || !plan || !plan.valid) {
      return;
    }
    var key = [
      plan.width,
      plan.height,
      settings.content,
      settings.seed,
      state.apiBase,
    ].join("|");
    if (key === state.previewKey) {
      return;
    }
    state.previewKey = key;
    elements.previewImage.src = previewUrl(
      plan.width,
      plan.height,
      settings.content,
      settings.seed,
      320
    );
  }

  function openPreview() {
    var plan = state.plan;
    if (!plan || !plan.valid || !state.settings) {
      showToast("请先完善生成参数。");
      return;
    }
    elements.previewLarge.src = previewUrl(
      plan.width,
      plan.height,
      state.settings.content,
      state.settings.seed,
      900
    );
    if (typeof elements.previewDialog.showModal === "function") {
      elements.previewDialog.showModal();
    } else {
      elements.previewDialog.setAttribute("open", "");
    }
  }

  function closePreview() {
    if (typeof elements.previewDialog.close === "function") {
      elements.previewDialog.close();
    } else {
      elements.previewDialog.removeAttribute("open");
    }
  }

  // -- batch --------------------------------------------------------------

  function makeSpec(settings, item) {
    var spec = {
      mode: item.mode,
      format: settings.format,
      quality: settings.quality,
      content: settings.content,
      probe: settings.probe,
      seed: settings.seed,
      aspect: settings.aspect,
    };
    if (item.mode === "bytes") {
      spec.target_bytes = item.targetBytes;
    } else {
      spec.width = item.width;
      spec.height = item.height;
    }
    return spec;
  }

  function updateBatchState() {
    var settings = readSettings();
    var parsed = TOOLS.parseBatch(elements.batchInput.value);
    var maximum = clampInt(
      (state.limits && state.limits.max_batch_items) || 50,
      50,
      1,
      500
    );
    var messages = [];

    if (parsed.invalid.length) {
      messages.push(
        "第 " +
          parsed.invalid
            .slice(0, 4)
            .map(function (item) {
              return item.line;
            })
            .join("、") +
          " 行格式无法识别。"
      );
    }
    if (parsed.specs.length > maximum) {
      messages.push("单次批量最多 " + maximum + " 张，当前 " + parsed.specs.length + " 张。");
    }

    parsed.specs.forEach(function (item, index) {
      if (messages.length >= 4) {
        return;
      }
      var check = TOOLS.planEstimate(
        Object.assign({}, planInput(settings), {
          mode: item.mode,
          width: item.width,
          height: item.height,
          targetBytes: item.targetBytes,
        })
      );
      if (!check.valid) {
        messages.push("第 " + (index + 1) + " 个规格：" + check.error);
      }
    });

    var valid = parsed.specs.length > 0 && messages.length === 0;
    setText(elements.batchCount, String(parsed.specs.length));
    elements.batchButton.disabled = !valid || state.generating;
    if (messages.length) {
      setText(elements.batchError, messages.join(" "));
      elements.batchError.hidden = false;
    } else {
      elements.batchError.hidden = true;
      setText(elements.batchError, "");
    }
    return valid ? parsed.specs : [];
  }

  function setGenerating(active) {
    state.generating = Boolean(active);
    elements.generateButton.disabled = state.generating || !state.planValid;
    elements.generateButton.classList.toggle("is-busy", state.generating);
    setText(elements.generateLabel, state.generating ? "生成中…" : "生成图片");
    elements.cancelGenerateButton.hidden = !(
      state.generating && state.generationJobId
    );
    elements.cancelGenerateButton.disabled = !state.generationJobId;
    updateBatchState();
  }

  function setSpeedRunning(active) {
    state.speedRunning = Boolean(active);
    elements.speedButton.disabled = state.speedRunning;
    elements.speedButton.classList.toggle("is-busy", state.speedRunning);
    setText(elements.speedLabel, state.speedRunning ? "测速中…" : "开始测速");
    elements.cancelSpeedButton.hidden = !(
      state.speedRunning && state.speedJobId
    );
    elements.cancelSpeedButton.disabled = !state.speedJobId;
  }

  // -- generation ---------------------------------------------------------

  function generationStageText(job) {
    var progress = job.progress || {};
    var total = Math.max(1, Number(progress.batch_total) || 1);
    var index = Math.max(1, Number(progress.batch_index) || 1);
    var phase = progress.phase;
    var prefix =
      total > 1 ? "第 " + index + " / " + total + " 张 · " : "";
    if (job.status === "queued") {
      return "排队中…";
    }
    if (progress.stage === "starting") {
      return prefix + "正在准备内容";
    }
    if (progress.stage === "saved") {
      return (
        prefix +
        "已保存" +
        (progress.filename ? " " + progress.filename : "")
      );
    }
    if (progress.stage === "done") {
      return total > 1 ? "全部 " + total + " 张已完成" : "已完成";
    }
    if (phase === "level1") {
      return (
        prefix +
        "一级估算第 " +
        (progress.iteration || 1) +
        " 轮 · " +
        (progress.width || "?") +
        "×" +
        (progress.height || "?")
      );
    }
    if (phase === "level2") {
      return (
        prefix +
        "二级校准第 " +
        (progress.iteration || 1) +
        " 轮 · 混合 " +
        Math.round((Number(progress.mix) || 0) * 100) +
        "%"
      );
    }
    return prefix + "正在编码";
  }

  function generationPercent(job) {
    var progress = job.progress || {};
    var total = Math.max(1, Number(progress.batch_total) || 1);
    var index = Math.max(1, Number(progress.batch_index) || 1);
    if (job.status === "done" || progress.stage === "done") {
      return 100;
    }
    var inner = 0.15;
    if (progress.stage === "saved") {
      inner = 1;
    } else if (progress.phase === "level1") {
      inner = 0.4;
    } else if (progress.phase === "level2") {
      inner = 0.75;
    }
    return Math.min(100, ((index - 1 + inner) / total) * 100);
  }

  function renderGenerationJob(job) {
    var progress = job.progress || {};
    var total = Math.max(1, Number(progress.batch_total) || 1);
    var index = Math.max(1, Number(progress.batch_index) || 1);
    elements.jobBanner.hidden = false;
    elements.jobBanner.classList.toggle(
      "is-error",
      job.status === "failed" || job.status === "cancelled"
    );
    setText(
      elements.jobTitle,
      job.status === "failed"
        ? "生成失败"
        : job.status === "cancelled"
        ? "已取消生成"
        : total > 1
        ? "正在批量生成"
        : "正在生成图片"
    );
    setText(elements.jobDetail, generationStageText(job));
    elements.jobProgressFill.style.width = generationPercent(job).toFixed(1) + "%";

    var stage =
      progress.phase === "level1"
        ? "一级估算"
        : progress.phase === "level2"
        ? "二级校准"
        : progress.stage === "saved"
        ? "保存文件"
        : progress.stage === "done"
        ? "完成"
        : "准备";
    setText(elements.jobStage, stage);
    var counter = "第 " + index + " / " + total + " 张";
    if (
      progress.deviation !== undefined &&
      progress.deviation !== null &&
      Number.isFinite(Number(progress.deviation))
    ) {
      counter +=
        " · 偏差 " + TOOLS.formatDeviation(Number(progress.deviation));
    } else if (progress.bytes) {
      counter += " · " + TOOLS.formatBytes(progress.bytes);
    }
    setText(elements.jobCounter, counter);
  }

  function clearGenerationTimer() {
    window.clearTimeout(state.generationTimer);
    state.generationTimer = null;
  }

  function startGenerationPolling(job) {
    clearGenerationTimer();
    state.generationJobId = job.id;
    setGenerating(true);
    renderGenerationJob(job);

    var failures = 0;
    async function tick() {
      if (!state.generationJobId) {
        return;
      }
      try {
        var data = await request(
          "api/jobs/" + encodeURIComponent(job.id)
        );
        if (!state.generationJobId) {
          return;
        }
        var current = data.job;
        if (!current) {
          throw new ApiError("任务不存在或已被清理。", "not_found", "api");
        }
        failures = 0;
        renderGenerationJob(current);
        if (current.status === "queued" || current.status === "running") {
          state.generationTimer = window.setTimeout(tick, 520);
          return;
        }
        finishGeneration(current);
      } catch (error) {
        if (!state.generationJobId) {
          return;
        }
        failures += 1;
        if (failures >= 3) {
          finishGeneration({
            id: job.id,
            status: "failed",
            error: error.message || "无法获取生成任务状态。",
            error_code: error.code || "poll_failed",
            progress: job.progress || {},
            result: null,
          });
          return;
        }
        state.generationTimer = window.setTimeout(tick, 900);
      }
    }
    state.generationTimer = window.setTimeout(tick, 320);
  }

  function finishGeneration(job) {
    if (!state.generationJobId) {
      return;
    }
    state.generationJobId = "";
    clearGenerationTimer();
    elements.jobBanner.hidden = true;
    setGenerating(false);

    var saved = 0;
    if (job.result && Array.isArray(job.result.entries)) {
      saved = job.result.entries.length;
    }
    if (job.status === "done") {
      showToast("已生成 " + saved + " 张图片。");
    } else if (job.status === "cancelled") {
      showToast("已取消生成" + (saved ? "，已保存 " + saved + " 张" : "") + "。");
    } else {
      setPlanError(job.error || "生成失败。");
      showToast("生成失败：" + (job.error || "未知错误"));
      elements.planError.scrollIntoView({ block: "nearest" });
    }
    loadLibrary().catch(function (error) {
      handleActionError(error, false);
    });
    if (job.status === "done") {
      setPlanError("");
    }
  }

  function buildSingleSpec(settings) {
    var plan = state.plan;
    if (!plan || !plan.valid) {
      throw new ApiError(plan && plan.error ? plan.error : "请先完善生成参数。", "invalid_spec");
    }
    var spec = {
      mode: settings.mode,
      format: settings.format,
      quality: settings.quality,
      content: settings.content,
      probe: settings.probe,
      seed: settings.seed,
    };
    if (settings.mode === "bytes") {
      spec.target_bytes = plan.targetBytes;
      spec.aspect = settings.aspect;
    } else {
      spec.width = settings.width;
      spec.height = settings.height;
    }
    return spec;
  }

  async function submitGeneration(specs) {
    setPlanError("");
    setGenerating(true);
    try {
      var data = await request("api/generate", {
        method: "POST",
        body: { specs: specs },
      });
      if (!data.job || !data.job.id) {
        throw new ApiError("服务没有返回生成任务。", "missing_job");
      }
      startGenerationPolling(data.job);
    } catch (error) {
      setGenerating(false);
      if (error && error.code === "already_running") {
        showToast(error.message || "已有生成任务在运行。");
        await resumeActiveJobs().catch(function () {});
        return;
      }
      handleActionError(error, true);
    }
  }

  async function cancelGeneration() {
    var jobId = state.generationJobId;
    if (!jobId) {
      return;
    }
    elements.cancelGenerateButton.disabled = true;
    try {
      await request("api/jobs/" + encodeURIComponent(jobId) + "/cancel", {
        method: "POST",
        body: {},
      });
      showToast("正在取消生成…");
    } catch (error) {
      elements.cancelGenerateButton.disabled = false;
      handleActionError(error, false);
    }
  }

  async function resumeActiveJobs(knownJobs) {
    var jobs = knownJobs;
    if (!jobs) {
      try {
        var health = await requestBase(state.apiBase, "health", {});
        jobs = health.jobs || {};
      } catch (error) {
        return;
      }
    }
    if (jobs.generate && !state.generationJobId) {
      try {
        var data = await request(
          "api/jobs/" + encodeURIComponent(jobs.generate)
        );
        if (
          data.job &&
          (data.job.status === "queued" || data.job.status === "running")
        ) {
          startGenerationPolling(data.job);
        }
      } catch (error) {
        /* The job may have finished between health and lookup. */
      }
    }
    if (jobs.speed && !state.speedJobId) {
      try {
        var speed = await request(
          "api/jobs/" + encodeURIComponent(jobs.speed)
        );
        if (
          speed.job &&
          (speed.job.status === "queued" || speed.job.status === "running")
        ) {
          startSpeedPolling(speed.job);
        }
      } catch (error) {
        /* The job may have finished between health and lookup. */
      }
    }
  }

  // -- library ------------------------------------------------------------

  function entryStatus(entry) {
    if (entry.target_bytes === null || entry.target_bytes === undefined) {
      return { cls: "is-neutral", label: "按分辨率" };
    }
    var deviation = Number(entry.deviation);
    var suffix = Number.isFinite(deviation)
      ? " " + TOOLS.formatDeviation(deviation)
      : "";
    if (entry.converged) {
      return { cls: "is-ok", label: "已收敛" + suffix };
    }
    return { cls: "is-bad", label: "未收敛" + suffix };
  }

  function fileCardHtml(entry) {
    var status = entryStatus(entry);
    var tags = [];
    if (entry.target_bytes) {
      tags.push("目标 " + TOOLS.formatBytes(entry.target_bytes));
    }
    tags.push("种子 " + entry.seed);
    if (entry.probe) {
      tags.push("降采样探针");
    }
    if (typeof entry.mix === "number" && entry.mix > 0) {
      tags.push("噪声混合 " + (entry.mix * 100).toFixed(1) + "%");
    }
    if (entry.iterations !== null && entry.iterations !== undefined) {
      tags.push("迭代 " + entry.iterations);
    }
    if (entry.elapsed_ms !== null && entry.elapsed_ms !== undefined) {
      tags.push("耗时 " + TOOLS.formatDurationMs(entry.elapsed_ms));
    }

    var quality =
      entry.format === "png" ? "无损" : "q" + entry.quality;
    var meta = [
      entry.width + " × " + entry.height,
      TOOLS.formatMp(entry.pixels),
      TOOLS.formatLabel(entry.format) + " " + quality,
      TOOLS.contentLabel(entry.content),
    ];

    return (
      '<article class="file-card" data-id="' +
      escapeHtml(entry.id) +
      '">' +
      '<div class="file-thumb" data-format="' +
      escapeHtml(entry.format) +
      '">' +
      escapeHtml(TOOLS.formatLabel(entry.format)) +
      "</div>" +
      '<div class="file-main">' +
      '<div class="file-title">' +
      '<strong title="' +
      escapeHtml(entry.filename) +
      '">' +
      escapeHtml(entry.filename) +
      "</strong>" +
      '<span class="badge ' +
      status.cls +
      '">' +
      escapeHtml(status.label) +
      "</span>" +
      "</div>" +
      '<div class="file-meta">' +
      meta
        .map(function (item) {
          return "<span>" + escapeHtml(item) + "</span>";
        })
        .join("") +
      "</div>" +
      '<div class="file-tags">' +
      tags
        .map(function (item) {
          return '<span class="file-tag">' + escapeHtml(item) + "</span>";
        })
        .join("") +
      "</div>" +
      (entry.note
        ? '<p class="file-note">' + escapeHtml(entry.note) + "</p>"
        : "") +
      "</div>" +
      '<div class="file-side">' +
      '<span class="file-size">' +
      escapeHtml(TOOLS.formatBytes(entry.bytes)) +
      "</span>" +
      '<span class="file-bytes">' +
      escapeHtml(TOOLS.formatBytesExact(entry.bytes)) +
      "</span>" +
      '<span class="file-time">' +
      escapeHtml(TOOLS.formatTimestamp(entry.created_at)) +
      "</span>" +
      '<div class="file-actions">' +
      '<button class="ghost-button" type="button" data-action="download">下载</button>' +
      '<button class="ghost-button danger" type="button" data-action="delete">删除</button>' +
      "</div>" +
      "</div>" +
      "</article>"
    );
  }

  function renderLibrary() {
    var entries = state.entries;
    var stats = state.stats || { count: 0, bytes: 0 };
    var targeted = TOOLS.convergedRate(entries);

    setText(elements.libraryCount, entries.length + " 个文件");
    if (entries.length) {
      setText(
        elements.librarySubtitle,
        "共 " +
          entries.length +
          " 个文件，累计 " +
          TOOLS.formatBytes(stats.bytes) +
          (targeted
            ? "；目标体积收敛 " +
              targeted.converged +
              " / " +
              targeted.total
            : "；按目标体积生成的样本会显示偏差。")
      );
    } else {
      setText(
        elements.librarySubtitle,
        "生成后会在这里显示真实像素、实际字节与偏差。"
      );
    }

    elements.libraryEmpty.hidden = entries.length > 0;
    elements.libraryList.innerHTML = entries.map(fileCardHtml).join("");
    elements.exportCsvButton.disabled = entries.length === 0;
    elements.bundleButton.disabled = entries.length === 0;
    elements.clearButton.disabled = entries.length === 0;

    setText(elements.summaryCount, TOOLS.formatCount(stats.count));
    setText(elements.summaryBytes, TOOLS.formatBytes(stats.bytes));
    if (targeted) {
      setText(
        elements.summaryConverged,
        TOOLS.formatPercent(targeted.ratio, 0)
      );
      setText(
        elements.summaryConvergedFoot,
        targeted.converged + " / " + targeted.total + " 个目标体积样本"
      );
    } else {
      setText(elements.summaryConverged, "—");
      setText(elements.summaryConvergedFoot, "暂无目标体积样本");
    }

    if (entries.length) {
      var latest = entries[0];
      setText(
        elements.summaryLatest,
        latest.width + "×" + latest.height
      );
      setText(
        elements.summaryLatestFoot,
        TOOLS.formatLabel(latest.format) +
          " · " +
          TOOLS.formatBytes(latest.bytes) +
          " · " +
          TOOLS.formatTimestamp(latest.created_at)
      );
    } else {
      setText(elements.summaryLatest, "—");
      setText(elements.summaryLatestFoot, "尚无记录");
    }
  }

  async function loadLibrary() {
    var data = await request("api/library");
    state.entries = Array.isArray(data.entries) ? data.entries : [];
    state.stats = data.stats || {
      count: state.entries.length,
      bytes: state.entries.reduce(function (sum, entry) {
        return sum + (Number(entry.bytes) || 0);
      }, 0),
    };
    if (data.limits) {
      state.options = state.options || {};
      state.options.library = Object.assign(
        {},
        state.options.library || {},
        data.limits
      );
      updateLibraryLimits();
    }
    renderLibrary();
  }

  function findEntry(id) {
    for (var index = 0; index < state.entries.length; index += 1) {
      if (state.entries[index].id === id) {
        return state.entries[index];
      }
    }
    return null;
  }

  async function deleteEntry(entry) {
    if (!entry) {
      return;
    }
    if (
      !window.confirm(
        "删除「" + entry.filename + "」？删除后无法恢复。"
      )
    ) {
      return;
    }
    try {
      await request("api/library/delete", {
        method: "POST",
        body: { id: entry.id },
      });
      showToast("已删除 " + entry.filename);
      await loadLibrary();
    } catch (error) {
      handleActionError(error, false);
    }
  }

  async function clearLibrary() {
    if (!state.entries.length) {
      return;
    }
    if (
      !window.confirm(
        "清空全部 " + state.entries.length + " 个生成文件？此操作无法撤销。"
      )
    ) {
      return;
    }
    try {
      var data = await request("api/library/clear", {
        method: "POST",
        body: {},
      });
      showToast("已清空 " + (data.removed || state.entries.length) + " 个文件。");
      await loadLibrary();
    } catch (error) {
      handleActionError(error, false);
    }
  }

  function exportCsv() {
    downloadPath(
      "api/manifest.csv",
      "img-lab-manifest-" + stamp() + ".csv"
    ).catch(function (error) {
      handleActionError(error, false);
    });
  }

  function downloadBundle() {
    downloadPath(
      "api/bundle.zip",
      "img-lab-images-" + stamp() + ".zip"
    ).catch(function (error) {
      handleActionError(error, false);
    });
  }

  function downloadEntry(entry) {
    if (!entry) {
      return;
    }
    downloadPath(
      "api/files/" + encodeURIComponent(entry.id),
      entry.filename
    ).catch(function (error) {
      handleActionError(error, false);
    });
  }

  // -- speed --------------------------------------------------------------

  function readSpeedSettings() {
    var url = elements.speedUrl.value.trim();
    var limits = state.speedLimits || {};
    var maxConcurrency = Number(limits.max_concurrency) || 16;
    var maxRounds = Number(limits.max_rounds) || 20;
    var maxRequests = Number(limits.max_requests) || 160;
    var concurrency = parseInt(elements.speedConcurrency.value, 10);
    var rounds = parseInt(elements.speedRounds.value, 10);
    var timeout = parseFloat(elements.speedTimeout.value);
    if (!url) {
      return { error: "请填写目标地址。" };
    }
    if (!/^https?:\/\//i.test(url)) {
      return { error: "目标地址需要以 http:// 或 https:// 开头。" };
    }
    try {
      new URL(url);
    } catch (error) {
      return { error: "目标地址格式不正确。" };
    }
    if (
      !Number.isInteger(concurrency) ||
      concurrency < 1 ||
      concurrency > maxConcurrency
    ) {
      return { error: "并发数必须介于 1 到 " + maxConcurrency + " 之间。" };
    }
    if (!Number.isInteger(rounds) || rounds < 1 || rounds > maxRounds) {
      return { error: "轮次必须介于 1 到 " + maxRounds + " 之间。" };
    }
    if (concurrency * rounds > maxRequests) {
      return {
        error: "总请求数 " + concurrency * rounds + " 超过上限 " + maxRequests + "。",
      };
    }
    if (!Number.isFinite(timeout) || timeout < 1 || timeout > 60) {
      return { error: "单请求超时必须介于 1 到 60 秒之间。" };
    }
    return {
      url: url,
      concurrency: concurrency,
      rounds: rounds,
      timeout: timeout,
    };
  }

  function setSpeedError(message) {
    if (message) {
      setText(elements.speedError, message);
      elements.speedError.hidden = false;
    } else {
      elements.speedError.hidden = true;
      setText(elements.speedError, "");
    }
  }

  function showSpeedFailure(message) {
    setText(elements.speedFailureText, message || "测速失败。");
    elements.speedFailure.hidden = false;
  }

  function hideSpeedFailure() {
    elements.speedFailure.hidden = true;
    setText(elements.speedFailureText, "");
  }

  function speedPercent(job) {
    var progress = job.progress || {};
    var total = Number(progress.total) || 0;
    var done = Number(progress.done) || 0;
    if (job.status === "done") {
      return 100;
    }
    return total ? Math.min(100, (done / total) * 100) : 0;
  }

  function renderSpeedJob(job) {
    var progress = job.progress || {};
    var total = Number(progress.total) || 0;
    var done = Number(progress.done) || 0;
    var ok = Number(progress.ok) || 0;
    var failed = Number(progress.failed) || 0;
    elements.speedJobBanner.hidden = false;
    elements.speedProgressFill.style.width =
      speedPercent(job).toFixed(1) + "%";
    setText(
      elements.speedJobDetail,
      total
        ? "已完成 " + done + " / " + total + "，成功 " + ok + "，失败 " + failed
        : "正在建立连接…"
    );
    setText(
      elements.speedJobStage,
      job.status === "queued" ? "排队中" : "发送 GET 请求"
    );
    setText(
      elements.speedJobCounter,
      total ? Math.round((done / total) * 100) + "%" : ""
    );
  }

  function clearSpeedTimer() {
    window.clearTimeout(state.speedTimer);
    state.speedTimer = null;
  }

  function startSpeedPolling(job) {
    clearSpeedTimer();
    state.speedJobId = job.id;
    setSpeedRunning(true);
    hideSpeedFailure();
    renderSpeedJob(job);

    var failures = 0;
    async function tick() {
      if (!state.speedJobId) {
        return;
      }
      try {
        var data = await request("api/jobs/" + encodeURIComponent(job.id));
        if (!state.speedJobId) {
          return;
        }
        var current = data.job;
        if (!current) {
          throw new ApiError("任务不存在或已被清理。", "not_found", "api");
        }
        failures = 0;
        renderSpeedJob(current);
        if (current.status === "queued" || current.status === "running") {
          state.speedTimer = window.setTimeout(tick, 700);
          return;
        }
        finishSpeed(current);
      } catch (error) {
        if (!state.speedJobId) {
          return;
        }
        failures += 1;
        if (failures >= 3) {
          finishSpeed({
            id: job.id,
            status: "failed",
            error: error.message || "无法获取测速任务状态。",
            progress: job.progress || {},
            result: null,
          });
          return;
        }
        state.speedTimer = window.setTimeout(tick, 1100);
      }
    }
    state.speedTimer = window.setTimeout(tick, 300);
  }

  function finishSpeed(job) {
    if (!state.speedJobId) {
      return;
    }
    state.speedJobId = "";
    clearSpeedTimer();
    elements.speedJobBanner.hidden = true;
    setSpeedRunning(false);

    if (job.result && (job.status === "done" || job.status === "cancelled")) {
      state.latestSpeed = job.result;
      renderSpeedResult(job.result);
      if (job.status === "cancelled") {
        showToast("测速已取消，显示已完成的样本。");
      } else {
        showToast("测速完成。");
      }
      return;
    }
    if (job.status === "cancelled") {
      showToast("测速已取消。");
      return;
    }
    if (state.latestSpeed) {
      renderSpeedResult(state.latestSpeed);
    } else {
      elements.speedEmpty.hidden = false;
      elements.speedSummary.hidden = true;
    }
    showSpeedFailure(job.error || "测速失败。");
    showToast("测速失败：" + (job.error || "未知错误"));
  }

  async function submitSpeed() {
    var settings = readSpeedSettings();
    if (settings.error) {
      setSpeedError(settings.error);
      return;
    }
    setSpeedError("");
    hideSpeedFailure();
    elements.speedEmpty.hidden = true;
    elements.speedSummary.hidden = true;
    setSpeedRunning(true);
    try {
      var data = await request("api/speedtest", {
        method: "POST",
        body: {
          url: settings.url,
          concurrency: settings.concurrency,
          rounds: settings.rounds,
          timeout: settings.timeout,
        },
      });
      if (!data.job || !data.job.id) {
        throw new ApiError("服务没有返回测速任务。", "missing_job");
      }
      startSpeedPolling(data.job);
    } catch (error) {
      setSpeedRunning(false);
      elements.speedEmpty.hidden = state.latestSpeed !== null;
      elements.speedSummary.hidden = state.latestSpeed === null;
      if (error && error.code === "already_running") {
        showToast(error.message || "已有测速任务在运行。");
        await resumeActiveJobs().catch(function () {});
        return;
      }
      handleActionError(error, false);
    }
  }

  async function cancelSpeed() {
    var jobId = state.speedJobId;
    if (!jobId) {
      return;
    }
    elements.cancelSpeedButton.disabled = true;
    try {
      await request("api/jobs/" + encodeURIComponent(jobId) + "/cancel", {
        method: "POST",
        body: {},
      });
      showToast("正在取消测速…");
    } catch (error) {
      elements.cancelSpeedButton.disabled = false;
      handleActionError(error, false);
    }
  }

  function renderTiming(stats) {
    var means = {};
    ["connect", "send", "wait", "download"].forEach(function (key) {
      means[key] = stats[key] ? Number(stats[key].mean) : 0;
    });
    var totalMean =
      stats.total && Number.isFinite(Number(stats.total.mean))
        ? Number(stats.total.mean)
        : means.connect + means.send + means.wait + means.download;

    elements.timingSegments.forEach(function (segment) {
      var key = segment.dataset.seg;
      var width = totalMean > 0 ? (means[key] / totalMean) * 100 : 0;
      segment.style.width = Math.max(0, width).toFixed(2) + "%";
    });
    elements.timingValues.forEach(function (node) {
      var key = node.dataset.value;
      setText(node, totalMean > 0 ? TOOLS.formatMs(means[key]) : "—");
    });
    setText(
      elements.timingTotal,
      totalMean > 0 ? "平均 " + TOOLS.formatMs(totalMean) : "—"
    );
    return { means: means, totalMean: totalMean };
  }

  function msOrDash(value) {
    if (value === null || value === undefined || value === "") {
      return "—";
    }
    return TOOLS.formatMs(value);
  }

  function renderSpeedResult(result) {
    elements.speedEmpty.hidden = true;
    elements.speedSummary.hidden = false;
    hideSpeedFailure();

    var total = Number(result.total) || 0;
    var ok = Number(result.ok) || 0;
    var failed = Number(result.failed) || 0;
    var completed = Number(result.completed) || 0;
    var stats = result.stats || {};

    setText(
      elements.speedBadge,
      result.cancelled
        ? "已取消 · " + completed + " / " + total
        : ok + " / " + total + " 成功"
    );
    setText(
      elements.speedSubtitle,
      (result.url || "") +
        " · " +
        result.concurrency +
        " 并发 × " +
        result.rounds +
        " 轮 · 总耗时 " +
        TOOLS.formatDurationMs(result.wall_ms)
    );

    var p50 = stats.total ? stats.total.p50 : null;
    var p95 = stats.total ? stats.total.p95 : null;
    setText(elements.speedP50, p50 === null ? "—" : TOOLS.formatMs(p50));
    setText(elements.speedP95, p95 === null ? "—" : TOOLS.formatMs(p95));
    setText(
      elements.speedP50Foot,
      completed + " 个完成样本 · " + total + " 个请求"
    );
    setText(
      elements.speedP95Foot,
      p95 === null ? "没有可用样本" : "长尾延迟"
    );

    if (stats.server) {
      setText(elements.speedServerP50, TOOLS.formatMs(stats.server.p50));
      setText(
        elements.speedServerFoot,
        "来自响应头 · " + TOOLS.formatMs(stats.server.mean) + " 平均"
      );
    } else {
      setText(elements.speedServerP50, "不可用");
      setText(elements.speedServerFoot, "目标未返回计时响应头");
    }

    setText(elements.speedFailRate, TOOLS.formatPercent(result.fail_rate, 1));
    setText(
      elements.speedFailFoot,
      ok + " 成功 / " + failed + " 失败 / " + total + " 请求"
    );

    var timing = renderTiming(stats);

    if (stats.server) {
      var serverMean = Number(stats.server.mean) || 0;
      var networkMean = Math.max(0, timing.totalMean - serverMean);
      var base = Math.max(timing.totalMean, serverMean, networkMean, 1);
      elements.networkBar.style.width = ((networkMean / base) * 100).toFixed(2) + "%";
      elements.serverBar.style.width = ((serverMean / base) * 100).toFixed(2) + "%";
      setText(elements.networkValue, TOOLS.formatMs(networkMean));
      setText(elements.serverValue, TOOLS.formatMs(serverMean));
      elements.serverCompare.hidden = false;
      setText(
        elements.serverNote,
        "服务器处理时间读取自目标响应头，包含至少一个可用样本。"
      );
      setText(
        elements.compareNote,
        "网络传输 = 总耗时 − 服务器处理，为近似拆分；服务端计时可能与网络等待重叠，仅供参考。"
      );
    } else {
      elements.serverCompare.hidden = true;
      elements.networkBar.style.width = "0%";
      elements.serverBar.style.width = "0%";
      setText(elements.networkValue, "—");
      setText(elements.serverValue, "—");
      setText(
        elements.serverNote,
        result.server_timing
          ? "响应头中的服务器计时样本不足，无法汇总。"
          : "目标未返回 Server-Timing、X-Response-Time 或 X-Runtime，服务器处理时间不可用。"
      );
    }

    elements.speedUrlLabel.innerHTML =
      '<span class="meta-pill"><strong>目标</strong> ' +
      escapeHtml(result.url || "") +
      "</span>";

    var statuses = result.statuses || {};
    var pills = [
      '<span class="meta-pill"><strong>请求</strong> ' +
        total +
        "（成功 " +
        ok +
        "）</span>",
    ];
    Object.keys(statuses).forEach(function (key) {
      var count = statuses[key];
      var label = /^\d+$/.test(key) ? "HTTP " + key : key;
      pills.push(
        '<span class="meta-pill">' + escapeHtml(label) + " × " + count + "</span>"
      );
    });
    elements.speedStatuses.innerHTML = pills.join("");

    var samples = Array.isArray(result.samples) ? result.samples : [];
    if (!samples.length) {
      elements.speedSamples.innerHTML =
        '<tr><td class="empty-cell" colspan="9">没有完成的样本。</td></tr>';
      return;
    }
    elements.speedSamples.innerHTML = samples
      .map(function (sample) {
        var okSample = Boolean(sample.ok);
        var statusText = sample.status
          ? String(sample.status)
          : sample.error_kind || "错误";
        var sizeText = TOOLS.formatBytes(sample.bytes);
        if (sample.truncated) {
          sizeText += " +";
        }
        return (
          "<tr>" +
          "<td>" +
          escapeHtml(sample.round) +
          "</td>" +
          "<td>" +
          escapeHtml(sample.worker) +
          "</td>" +
          '<td class="' +
          (okSample ? "status-ok" : "status-bad") +
          '" title="' +
          escapeHtml(sample.error || "") +
          '">' +
          escapeHtml(statusText) +
          "</td>" +
          '<td class="num">' +
          msOrDash(sample.total_ms) +
          "</td>" +
          '<td class="num">' +
          msOrDash(sample.connect_ms) +
          "</td>" +
          '<td class="num">' +
          msOrDash(sample.wait_ms) +
          "</td>" +
          '<td class="num">' +
          msOrDash(sample.download_ms) +
          "</td>" +
          '<td class="num">' +
          (sample.server_ms === null || sample.server_ms === undefined
            ? "—"
            : TOOLS.formatMs(sample.server_ms)) +
          "</td>" +
          '<td class="num" title="' +
          escapeHtml(TOOLS.formatBytesExact(sample.bytes)) +
          '">' +
          escapeHtml(sizeText) +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  // -- shared errors and toast -------------------------------------------

  function showToast(message) {
    window.clearTimeout(state.toastTimer);
    setText(elements.toast, message);
    elements.toast.hidden = false;
    state.toastTimer = window.setTimeout(function () {
      elements.toast.hidden = true;
    }, 3600);
  }

  function handleActionError(error, inlinePlan) {
    var message =
      error && error.message ? error.message : "操作失败，请稍后重试。";
    if (error && (error.kind === "connection" || error.kind === "login")) {
      showBanner(message);
    } else if (inlinePlan) {
      setPlanError(message);
    } else {
      showToast(message);
    }
    if (error && error.kind === "connection") {
      setConnection("offline");
    }
    if (window.console && console.warn) {
      console.warn(error);
    }
  }

  // -- boot and events ----------------------------------------------------

  async function boot() {
    hideBanner();
    setConnection("pending");
    try {
      var health = await probeBase();
      updateVersion(health.version);
      var data = await request("api/options");
      applyOptions(data);
      await loadLibrary();
      hideBanner();
      state.booted = true;
      updatePlan();
      updateBatchState();
      await resumeActiveJobs(health.jobs || {});
    } catch (error) {
      setConnection(state.apiBase ? "online" : "offline");
      showBanner(error && error.message ? error.message : "应用初始化失败。");
      renderLibrary();
    }
  }

  function bindEvents() {
    elements.appRetryButton.addEventListener("click", function () {
      boot();
    });

    elements.themeToggle.addEventListener("click", function () {
      var theme = window.ImgLabTheme;
      if (!theme) {
        return;
      }
      var mode = theme.cycle();
      updateThemeLabel();
      showToast(theme.label(mode));
    });
    window.addEventListener("imglab:themechange", updateThemeLabel);

    elements.tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        setTab(tab.dataset.tab);
      });
    });
    document
      .querySelector(".tabs")
      .addEventListener("keydown", function (event) {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
          return;
        }
        event.preventDefault();
        var current =
          document.activeElement === elements.tabSpeed
            ? elements.tabSpeed
            : elements.tabGenerator;
        var next =
          current === elements.tabGenerator
            ? elements.tabSpeed
            : elements.tabGenerator;
        setTab(next.dataset.tab);
        next.focus();
      });

    elements.modeButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        setMode(button.dataset.mode, 0);
      });
    });

    elements.widthInput.addEventListener("input", function () {
      schedulePlan();
    });
    elements.heightInput.addEventListener("input", function () {
      schedulePlan();
    });
    elements.targetSize.addEventListener("input", function () {
      schedulePlan();
    });
    elements.aspectSelect.addEventListener("change", function () {
      schedulePlan(0);
    });
    elements.seedInput.addEventListener("input", function () {
      schedulePlan();
    });
    elements.probeToggle.addEventListener("change", function () {
      updateBatchState();
      updatePlan();
    });

    document.querySelectorAll(".preset-row").forEach(function (row) {
      row.addEventListener("click", function (event) {
        var button = event.target.closest("button.chip");
        if (!button) {
          return;
        }
        if (button.dataset.width) {
          elements.widthInput.value = button.dataset.width;
          elements.heightInput.value = button.dataset.height;
          setMode("resolution", 0);
        } else if (button.dataset.size) {
          elements.targetSize.value = button.dataset.size;
          setMode("bytes", 0);
        }
      });
    });

    elements.formatSelect.addEventListener("change", function () {
      updateFormatUi();
      updateBatchState();
      updatePlan();
    });

    elements.qualityRange.addEventListener("input", function () {
      elements.qualityInput.value = elements.qualityRange.value;
      schedulePlan();
    });
    elements.qualityInput.addEventListener("input", function () {
      var value = parseInt(elements.qualityInput.value, 10);
      if (Number.isFinite(value) && value >= 1 && value <= 100) {
        elements.qualityRange.value = String(value);
      }
      schedulePlan();
    });
    elements.qualityInput.addEventListener("blur", function () {
      var value = clampInt(elements.qualityInput.value, 85, 1, 100);
      elements.qualityInput.value = String(value);
      elements.qualityRange.value = String(value);
      schedulePlan(0);
    });

    elements.contentOptions.addEventListener("change", function () {
      syncContentCards();
      schedulePlan(0);
    });

    elements.batchInput.addEventListener("input", function () {
      updateBatchState();
    });
    elements.batchButton.addEventListener("click", function () {
      var specs = updateBatchState();
      if (!specs.length) {
        return;
      }
      var settings = readSettings();
      var payload = specs.map(function (item) {
        return makeSpec(settings, item);
      });
      submitGeneration(payload);
    });

    elements.generateForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (state.generating) {
        return;
      }
      var settings = readSettings();
      updatePlan();
      if (!state.planValid) {
        setPlanError(
          (state.plan && state.plan.error) || "请先修正生成参数。"
        );
        return;
      }
      try {
        submitGeneration([buildSingleSpec(settings)]);
      } catch (error) {
        handleActionError(error, true);
      }
    });
    elements.cancelGenerateButton.addEventListener("click", function () {
      cancelGeneration();
    });

    elements.previewButton.addEventListener("click", openPreview);
    elements.previewClose.addEventListener("click", closePreview);
    elements.previewDialog.addEventListener("click", function (event) {
      if (event.target === elements.previewDialog) {
        closePreview();
      }
    });
    elements.previewDialog.addEventListener("close", function () {
      elements.previewLarge.removeAttribute("src");
    });
    elements.previewImage.addEventListener("load", function () {
      elements.previewButton.classList.add("has-image");
      setText(elements.previewFallback, "预览");
    });
    elements.previewImage.addEventListener("error", function () {
      if (!elements.previewImage.getAttribute("src")) {
        return;
      }
      elements.previewButton.classList.remove("has-image");
      setText(elements.previewFallback, "预览不可用");
      state.previewKey = "";
    });

    elements.exportCsvButton.addEventListener("click", exportCsv);
    elements.bundleButton.addEventListener("click", downloadBundle);
    elements.clearButton.addEventListener("click", clearLibrary);
    elements.libraryList.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-action]");
      if (!button) {
        return;
      }
      var card = button.closest(".file-card");
      if (!card) {
        return;
      }
      var entry = findEntry(card.dataset.id);
      if (button.dataset.action === "download") {
        downloadEntry(entry);
      } else if (button.dataset.action === "delete") {
        deleteEntry(entry);
      }
    });

    elements.speedForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (state.speedRunning) {
        return;
      }
      submitSpeed();
    });
    elements.cancelSpeedButton.addEventListener("click", function () {
      cancelSpeed();
    });
    [elements.speedConcurrency, elements.speedRounds].forEach(function (
      input
    ) {
      input.addEventListener("input", updateSpeedHint);
    });
    elements.speedUrl.addEventListener("input", function () {
      setSpeedError("");
    });

    window.addEventListener("beforeunload", function () {
      clearGenerationTimer();
      clearSpeedTimer();
      window.clearTimeout(state.toastTimer);
      window.clearTimeout(state.previewTimer);
      window.clearTimeout(state.planTimer);
    });
  }

  function init() {
    cacheElements();
    bindEvents();
    syncContentCards();
    setTab("generator");
    updateModeUi();
    updateFormatUi();
    updateThemeLabel();
    updateSpeedHint();
    renderLibrary();
    setGenerating(false);
    setSpeedRunning(false);
    updatePlan();
    boot();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

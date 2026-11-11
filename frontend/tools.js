(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.ImgLabTools = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MIB = 1024 * 1024;
  var MIN_DIMENSION = 128;
  var MAX_DIMENSION = 16384;
  var ALIGN = 8;
  var MAX_PIXELS = 60000000;
  var MAX_OUTPUT_BYTES = 256 * MIB;
  var MAX_BATCH_ITEMS = 50;

  var JPEG_CURVE = [
    [1, 0.017],
    [25, 0.039],
    [50, 0.081],
    [60, 0.101],
    [70, 0.13],
    [75, 0.15],
    [80, 0.185],
    [85, 0.239],
    [90, 0.331],
    [95, 0.524],
    [100, 1.038],
  ];
  var WEBP_CURVE = [
    [1, 0.011],
    [25, 0.027],
    [50, 0.066],
    [60, 0.085],
    [70, 0.119],
    [75, 0.134],
    [80, 0.183],
    [85, 0.257],
    [90, 0.346],
    [95, 0.487],
    [100, 0.627],
  ];
  var CONTENT_FACTORS = {
    jpeg: { photo: 1, gradient: 0.088, solid: 0.066, noise: 3.172 },
    webp: { photo: 1, gradient: 0.014, solid: 0.007, noise: 2.928 },
  };
  var PNG_BPP = { photo: 1.92, gradient: 0.022, solid: 0.004, noise: 3.0 };

  var CONTENT_LABELS = {
    photo: "照片纹理",
    gradient: "平滑渐变",
    solid: "纯色块",
    noise: "高频噪声",
  };
  var FORMAT_LABELS = {
    jpeg: "JPEG",
    png: "PNG",
    webp: "WebP",
  };
  var STATUS_LABELS = {
    queued: "排队中",
    running: "进行中",
    done: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  var DEVIATION_LABELS = {
    none: "不适用",
    converged: "已收敛",
    near: "接近目标",
    off: "未达容差",
  };

  function pad2(value) {
    return value < 10 ? "0" + value : String(value);
  }

  function numberOr(value, fallback) {
    var number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function groupDigits(value) {
    var text = String(Math.round(Number(value)));
    if (!/^-?\d+$/.test(text)) {
      return String(value);
    }
    var negative = text.charAt(0) === "-";
    var digits = negative ? text.slice(1) : text;
    var grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return negative ? "-" + grouped : grouped;
  }

  function formatCount(value) {
    var number = Number(value);
    if (!Number.isFinite(number)) {
      return "—";
    }
    return groupDigits(number);
  }

  function formatBytes(bytes) {
    var value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) {
      return "—";
    }
    if (value < 1024) {
      return Math.round(value) + " B";
    }
    var units = ["KiB", "MiB", "GiB", "TiB"];
    var size = value / 1024;
    var index = 0;
    while (size >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    var decimals = size >= 100 ? 0 : size >= 10 ? 1 : 2;
    return size.toFixed(decimals) + " " + units[index];
  }

  function formatBytesExact(bytes) {
    var value = Number(bytes);
    if (
      bytes === null ||
      bytes === undefined ||
      !Number.isFinite(value) ||
      value < 0
    ) {
      return "—";
    }
    return groupDigits(value) + " 字节";
  }

  function formatMp(pixels) {
    var value = Number(pixels);
    if (!Number.isFinite(value) || value < 0) {
      return "—";
    }
    var decimals = value >= 10000000 ? 1 : 2;
    return (value / 1000000).toFixed(decimals) + " MP";
  }

  function formatPercent(ratio, digits) {
    var value = Number(ratio);
    if (
      ratio === null ||
      ratio === undefined ||
      !Number.isFinite(value)
    ) {
      return "—";
    }
    var places = Number.isInteger(digits) ? digits : 2;
    return (value * 100).toFixed(places) + "%";
  }

  function formatDeviation(ratio) {
    var value = Number(ratio);
    if (
      ratio === null ||
      ratio === undefined ||
      !Number.isFinite(value)
    ) {
      return "—";
    }
    var percent = value * 100;
    var sign = percent > 0 ? "+" : percent < 0 ? "-" : "";
    return sign + Math.abs(percent).toFixed(2) + "%";
  }

  function formatMs(milliseconds) {
    var value = Number(milliseconds);
    if (
      milliseconds === null ||
      milliseconds === undefined ||
      !Number.isFinite(value)
    ) {
      return "—";
    }
    if (value < 1) {
      return "<1 ms";
    }
    if (value < 1000) {
      return (value >= 100 ? value.toFixed(0) : value.toFixed(1)) + " ms";
    }
    return (value / 1000).toFixed(2) + " s";
  }

  function formatDurationMs(milliseconds) {
    var value = Number(milliseconds);
    if (!Number.isFinite(value) || value < 0) {
      return "—";
    }
    if (value < 1000) {
      return "<1 秒";
    }
    var seconds = value / 1000;
    if (seconds < 60) {
      return seconds.toFixed(seconds >= 10 ? 0 : 1) + " 秒";
    }
    var minutes = Math.floor(seconds / 60);
    var rest = Math.round(seconds - minutes * 60);
    if (rest === 60) {
      minutes += 1;
      rest = 0;
    }
    return minutes + " 分" + (rest ? " " + rest + " 秒" : "");
  }

  function formatTimestamp(iso) {
    if (!iso) {
      return "—";
    }
    var date = new Date(String(iso));
    if (Number.isNaN(date.getTime())) {
      return "—";
    }
    return (
      date.getFullYear() +
      "-" +
      pad2(date.getMonth() + 1) +
      "-" +
      pad2(date.getDate()) +
      " " +
      pad2(date.getHours()) +
      ":" +
      pad2(date.getMinutes()) +
      ":" +
      pad2(date.getSeconds())
    );
  }

  function deviationStatus(deviation, tolerance) {
    if (deviation === null || deviation === undefined || deviation === "") {
      return "none";
    }
    var value = Number(deviation);
    if (!Number.isFinite(value)) {
      return "none";
    }
    var limit = numberOr(tolerance, 0.015);
    if (limit <= 0) {
      limit = 0.015;
    }
    var absolute = Math.abs(value);
    if (absolute <= limit) {
      return "converged";
    }
    if (absolute <= limit * 4) {
      return "near";
    }
    return "off";
  }

  function deviationLabel(status) {
    return DEVIATION_LABELS[status] || DEVIATION_LABELS.none;
  }

  function statusLabel(status) {
    return STATUS_LABELS[status] || String(status || "未知");
  }

  function contentLabel(content) {
    return CONTENT_LABELS[content] || String(content || "");
  }

  function formatLabel(format) {
    return FORMAT_LABELS[format] || String(format || "").toUpperCase();
  }

  function interpolateCurve(curve, quality) {
    if (!Array.isArray(curve) || !curve.length) {
      return 0.215;
    }
    var value = clamp(Math.round(numberOr(quality, 85)), 1, 100);
    if (value <= curve[0][0]) {
      return Number(curve[0][1]);
    }
    if (value >= curve[curve.length - 1][0]) {
      return Number(curve[curve.length - 1][1]);
    }
    for (var index = 0; index < curve.length - 1; index += 1) {
      var start = curve[index];
      var end = curve[index + 1];
      if (value >= start[0] && value <= end[0]) {
        var ratio = (value - start[0]) / (end[0] - start[0]);
        return Number(start[1]) + (Number(end[1]) - Number(start[1])) * ratio;
      }
    }
    return Number(curve[curve.length - 1][1]);
  }

  function priorBpp(format, content, quality, priors) {
    var source = priors || {};
    if (format === "png") {
      var pngTable = source.pngBpp || PNG_BPP;
      return Math.max(0.002, numberOr(pngTable[content], 1.3));
    }
    var curve =
      format === "webp"
        ? source.webpQualityCurve || WEBP_CURVE
        : source.jpegQualityCurve || JPEG_CURVE;
    var factors = (source.contentFactors || CONTENT_FACTORS)[format] || {};
    var base = interpolateCurve(curve, quality);
    return Math.max(0.002, base * numberOr(factors[content], 1));
  }

  function clampDimension(value) {
    var number = Math.round(numberOr(value, MIN_DIMENSION));
    if (number < MIN_DIMENSION) {
      return MIN_DIMENSION;
    }
    if (number > MAX_DIMENSION) {
      return MAX_DIMENSION;
    }
    var aligned = Math.round(number / ALIGN) * ALIGN;
    return clamp(aligned, MIN_DIMENSION, MAX_DIMENSION);
  }

  function dimensionsForPixels(pixels, aspect) {
    var ratio = numberOr(aspect, 4 / 3);
    if (ratio <= 0) {
      ratio = 4 / 3;
    }
    ratio = clamp(ratio, 0.2, 5);
    var area = Math.max(MIN_DIMENSION * MIN_DIMENSION, numberOr(pixels, 0));
    var height = Math.sqrt(area / ratio);
    var width = height * ratio;
    return {
      width: clampDimension(Math.round(width)),
      height: clampDimension(Math.round(height)),
    };
  }

  function aspectLabel(aspect) {
    var value = numberOr(aspect, 4 / 3);
    var presets = [
      [4 / 3, "4:3"],
      [3 / 2, "3:2"],
      [16 / 9, "16:9"],
      [1, "1:1"],
      [9 / 16, "9:16"],
      [2 / 3, "2:3"],
      [3 / 4, "3:4"],
    ];
    for (var index = 0; index < presets.length; index += 1) {
      if (Math.abs(value - presets[index][0]) < 0.02) {
        return presets[index][1];
      }
    }
    return "≈" + value.toFixed(2) + ":1";
  }

  function maxPixelsFor(format, priors) {
    var source = priors || {};
    var table = source.formatMaxPixels || {
      jpeg: MAX_PIXELS,
      webp: MAX_PIXELS,
      png: 25000000,
    };
    return Math.min(numberOr(table[format], MAX_PIXELS), MAX_PIXELS);
  }

  function slowFormatWarning(format, pixels) {
    if (format === "webp" && pixels > 25000000) {
      return "WebP 超过 25 MP 后编码明显变慢，48 MP 可能超过 10 秒。";
    }
    return "";
  }

  function planEstimate(input) {
    var options = input || {};
    var format = options.format || "jpeg";
    var content = options.content || "photo";
    var quality = clamp(Math.round(numberOr(options.quality, 85)), 1, 100);
    var priors = options.priors || {};
    var aspect = numberOr(options.aspect, 4 / 3);
    if (aspect <= 0) {
      aspect = 4 / 3;
    }
    var maxPixels = numberOr(options.maxPixels, maxPixelsFor(format, priors));
    maxPixels = Math.min(maxPixels, MAX_PIXELS);
    var bpp = priorBpp(format, content, quality, priors);
    var result = {
      valid: true,
      error: "",
      note: "",
      mode: options.mode === "bytes" ? "bytes" : "resolution",
      format: format,
      content: content,
      quality: quality,
      aspect: aspect,
      bpp: bpp,
      width: 0,
      height: 0,
      pixels: 0,
      megapixels: 0,
      estimateBytes: 0,
      targetBytes: null,
      clamped: false,
    };

    if (result.mode === "resolution") {
      var width = Math.round(numberOr(options.width, 0));
      var height = Math.round(numberOr(options.height, 0));
      if (width <= 0 || height <= 0) {
        result.valid = false;
        result.error = "请填写有效的宽度和高度。";
        return result;
      }
      if (width < MIN_DIMENSION || height < MIN_DIMENSION) {
        result.valid = false;
        result.error = "宽高不能小于 " + MIN_DIMENSION + " px。";
        return result;
      }
      if (width > MAX_DIMENSION || height > MAX_DIMENSION) {
        result.valid = false;
        result.error = "宽高不能大于 " + MAX_DIMENSION + " px。";
        return result;
      }
      if (width * height > maxPixels) {
        result.valid = false;
        result.error =
          formatLabel(format) +
          " 最多 " +
          formatMp(maxPixels) +
          "，请降低分辨率或换用 JPEG / WebP。";
        return result;
      }
      result.width = width;
      result.height = height;
      result.pixels = width * height;
      result.megapixels = result.pixels / 1000000;
      result.estimateBytes = Math.round(result.pixels * bpp);
      result.note = slowFormatWarning(format, result.pixels);
      return result;
    }

    var target = Math.round(numberOr(options.targetBytes, 0));
    if (target < 1024) {
      result.valid = false;
      result.error = "目标体积至少为 1 KiB。";
      return result;
    }
    if (target > MAX_OUTPUT_BYTES) {
      result.valid = false;
      result.error = "目标体积最多为 256 MiB。";
      return result;
    }
    var pixels = target / bpp;
    if (pixels > maxPixels) {
      pixels = maxPixels;
      result.clamped = true;
      result.note =
        "目标体积超出当前格式的像素上限，生成结果会停在约 " +
        formatMp(maxPixels) +
        " 并明确标记未收敛。";
    }
    var dimensions = dimensionsForPixels(pixels, aspect);
    result.width = dimensions.width;
    result.height = dimensions.height;
    result.pixels = dimensions.width * dimensions.height;
    result.megapixels = result.pixels / 1000000;
    result.estimateBytes = target;
    result.targetBytes = target;
    var warning = slowFormatWarning(format, result.pixels);
    if (warning) {
      result.note = result.note ? result.note + " " + warning : warning;
    }
    return result;
  }

  function parseTargetBytes(text) {
    var raw = String(text === null || text === undefined ? "" : text)
      .trim()
      .replace(/,/g, "");
    var match = raw.match(/^([0-9]+(?:\.[0-9]+)?)\s*(b|kb|kib|mb|mib|gb|gib)?$/i);
    if (!match) {
      return null;
    }
    var value = parseFloat(match[1]);
    if (!Number.isFinite(value) || value <= 0) {
      return null;
    }
    var unit = (match[2] || "mib").toLowerCase();
    var factors = {
      b: 1,
      kb: 1000,
      kib: 1024,
      mb: 1000000,
      mib: MIB,
      gb: 1000000000,
      gib: 1024 * MIB,
    };
    var bytes = Math.round(value * (factors[unit] || MIB));
    return bytes >= 1 ? bytes : null;
  }

  function parseSpecLine(line) {
    var text = String(line === null || line === undefined ? "" : line).trim();
    if (!text) {
      return null;
    }
    var dimensions = text.match(/^(\d{1,5})\s*(?:x|×|\*)\s*(\d{1,5})$/i);
    if (dimensions) {
      return {
        mode: "resolution",
        width: parseInt(dimensions[1], 10),
        height: parseInt(dimensions[2], 10),
      };
    }
    var bytes = parseTargetBytes(text);
    if (bytes !== null) {
      return { mode: "bytes", targetBytes: bytes };
    }
    return null;
  }

  function parseBatch(text) {
    var specs = [];
    var invalid = [];
    String(text === null || text === undefined ? "" : text)
      .split(/\r?\n/)
      .forEach(function (line, index) {
        var trimmed = line.trim();
        if (!trimmed) {
          return;
        }
        var parsed = parseSpecLine(trimmed);
        if (parsed) {
          specs.push(parsed);
        } else {
          invalid.push({ line: index + 1, text: trimmed });
        }
      });
    return { specs: specs, invalid: invalid };
  }

  function summariseSpec(spec, options) {
    var source = spec || {};
    var settings = options || {};
    var parts = [];
    if (source.mode === "resolution") {
      parts.push(source.width + "×" + source.height);
    } else {
      parts.push(formatBytes(source.targetBytes) + " 目标");
    }
    parts.push(
      formatLabel(settings.format || "jpeg") +
        " " +
        (String(settings.format || "jpeg") === "png"
          ? "无损"
          : "q" + numberOr(settings.quality, 85))
    );
    parts.push(contentLabel(settings.content || "photo"));
    if (settings.probe) {
      parts.push("探针");
    }
    return parts.join(" · ");
  }

  function convergedRate(entries) {
    var list = Array.isArray(entries) ? entries : [];
    var targeted = list.filter(function (entry) {
      return entry && Number(entry.target_bytes) > 0;
    });
    if (!targeted.length) {
      return null;
    }
    var converged = targeted.filter(function (entry) {
      return Boolean(entry.converged);
    }).length;
    return {
      total: targeted.length,
      converged: converged,
      ratio: converged / targeted.length,
    };
  }

  function escapeHtml(text) {
    var map = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return String(text === null || text === undefined ? "" : text).replace(
      /[&<>"']/g,
      function (character) {
        return map[character];
      }
    );
  }

  function getCookieValue(cookieString, name) {
    var source = String(cookieString || "");
    var prefix = encodeURIComponent(name) + "=";
    var items = source.split(";");
    for (var index = 0; index < items.length; index += 1) {
      var item = items[index].trim();
      if (item.indexOf(prefix) === 0) {
        return decodeURIComponent(item.slice(prefix.length));
      }
    }
    return "";
  }

  return {
    MIB: MIB,
    MIN_DIMENSION: MIN_DIMENSION,
    MAX_DIMENSION: MAX_DIMENSION,
    ALIGN: ALIGN,
    MAX_PIXELS: MAX_PIXELS,
    MAX_OUTPUT_BYTES: MAX_OUTPUT_BYTES,
    MAX_BATCH_ITEMS: MAX_BATCH_ITEMS,
    CONTENT_LABELS: CONTENT_LABELS,
    FORMAT_LABELS: FORMAT_LABELS,
    formatCount: formatCount,
    formatBytes: formatBytes,
    formatBytesExact: formatBytesExact,
    formatMp: formatMp,
    formatPercent: formatPercent,
    formatDeviation: formatDeviation,
    formatMs: formatMs,
    formatDurationMs: formatDurationMs,
    formatTimestamp: formatTimestamp,
    deviationStatus: deviationStatus,
    deviationLabel: deviationLabel,
    statusLabel: statusLabel,
    contentLabel: contentLabel,
    formatLabel: formatLabel,
    interpolateCurve: interpolateCurve,
    priorBpp: priorBpp,
    clampDimension: clampDimension,
    dimensionsForPixels: dimensionsForPixels,
    aspectLabel: aspectLabel,
    maxPixelsFor: maxPixelsFor,
    planEstimate: planEstimate,
    parseTargetBytes: parseTargetBytes,
    parseSpecLine: parseSpecLine,
    parseBatch: parseBatch,
    summariseSpec: summariseSpec,
    convergedRate: convergedRate,
    escapeHtml: escapeHtml,
    getCookieValue: getCookieValue,
  };
});

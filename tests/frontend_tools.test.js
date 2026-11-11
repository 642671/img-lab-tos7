"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const TOOLS = require("../frontend/tools.js");

test("constants mirror the backend limits", () => {
  assert.equal(TOOLS.MIB, 1024 * 1024);
  assert.equal(TOOLS.MIN_DIMENSION, 128);
  assert.equal(TOOLS.MAX_DIMENSION, 16384);
  assert.equal(TOOLS.ALIGN, 8);
  assert.equal(TOOLS.MAX_PIXELS, 60000000);
  assert.equal(TOOLS.MAX_OUTPUT_BYTES, 256 * TOOLS.MIB);
  assert.equal(TOOLS.MAX_BATCH_ITEMS, 50);
});

test("formatBytes switches units at 1024 boundaries", () => {
  assert.equal(TOOLS.formatBytes(0), "0 B");
  assert.equal(TOOLS.formatBytes(1023), "1023 B");
  assert.equal(TOOLS.formatBytes(1024), "1.00 KiB");
  assert.equal(TOOLS.formatBytes(1536), "1.50 KiB");
  assert.equal(TOOLS.formatBytes(5 * 1024 * 1024), "5.00 MiB");
  assert.equal(TOOLS.formatBytes(8 * 1024 * 1024), "8.00 MiB");
  assert.equal(TOOLS.formatBytes(200 * 1024 * 1024), "200 MiB");
  assert.equal(TOOLS.formatBytes(-1), "—");
  assert.equal(TOOLS.formatBytes("x"), "—");
});

test("formatBytesExact groups digits", () => {
  assert.equal(TOOLS.formatBytesExact(1043372), "1,043,372 字节");
  assert.equal(TOOLS.formatBytesExact(999), "999 字节");
  assert.equal(TOOLS.formatBytesExact(null), "—");
});

test("formatCount groups and guards", () => {
  assert.equal(TOOLS.formatCount(1234567), "1,234,567");
  assert.equal(TOOLS.formatCount(12), "12");
  assert.equal(TOOLS.formatCount(undefined), "—");
});

test("formatMp uses one decimal above 10 MP", () => {
  assert.equal(TOOLS.formatMp(4366000), "4.37 MP");
  assert.equal(TOOLS.formatMp(48000000), "48.0 MP");
  assert.equal(TOOLS.formatMp(-5), "—");
});

test("formatPercent and formatDeviation render ratios as percentages", () => {
  assert.equal(TOOLS.formatPercent(0.5), "50.00%");
  assert.equal(TOOLS.formatPercent(0.5, 0), "50%");
  assert.equal(TOOLS.formatDeviation(0.0123), "+1.23%");
  assert.equal(TOOLS.formatDeviation(-0.0123), "-1.23%");
  assert.equal(TOOLS.formatDeviation(0), "0.00%");
  assert.equal(TOOLS.formatDeviation(null), "—");
});

test("formatMs switches between ms and s", () => {
  assert.equal(TOOLS.formatMs(0.5), "<1 ms");
  assert.equal(TOOLS.formatMs(12.34), "12.3 ms");
  assert.equal(TOOLS.formatMs(150), "150 ms");
  assert.equal(TOOLS.formatMs(2500), "2.50 s");
  assert.equal(TOOLS.formatMs(null), "—");
});

test("formatDurationMs describes seconds and minutes", () => {
  assert.equal(TOOLS.formatDurationMs(500), "<1 秒");
  assert.equal(TOOLS.formatDurationMs(3500), "3.5 秒");
  assert.equal(TOOLS.formatDurationMs(15000), "15 秒");
  assert.equal(TOOLS.formatDurationMs(61000), "1 分 1 秒");
  assert.equal(TOOLS.formatDurationMs(120000), "2 分");
});

test("priorBpp matches the calibrated JPEG curve at q85", () => {
  assert.ok(Math.abs(TOOLS.priorBpp("jpeg", "photo", 85) - 0.239) < 1e-9);
  assert.ok(Math.abs(TOOLS.priorBpp("webp", "photo", 85) - 0.257) < 1e-9);
  assert.ok(Math.abs(TOOLS.priorBpp("png", "photo", 85) - 1.92) < 1e-9);
  assert.ok(TOOLS.priorBpp("jpeg", "noise", 85) > 0.5);
  assert.ok(TOOLS.priorBpp("jpeg", "solid", 85) < 0.05);
  assert.ok(TOOLS.priorBpp("jpeg", "photo", 95) > TOOLS.priorBpp("jpeg", "photo", 50));
});

test("priorBpp honours priors supplied by the backend", () => {
  const priors = {
    jpegQualityCurve: [[85, 0.5]],
    contentFactors: { jpeg: { photo: 2 } },
    pngBpp: { photo: 3 },
  };
  assert.equal(TOOLS.priorBpp("jpeg", "photo", 85, priors), 1);
  assert.equal(TOOLS.priorBpp("png", "photo", 85, priors), 3);
});

test("clampDimension aligns to 8 and clamps the range", () => {
  assert.equal(TOOLS.clampDimension(100), 128);
  assert.equal(TOOLS.clampDimension(20000), 16384);
  assert.equal(TOOLS.clampDimension(1000.4), 1000);
  assert.equal(TOOLS.clampDimension("abc"), 128);
});

test("dimensionsForPixels keeps area and aspect", () => {
  const dims = TOOLS.dimensionsForPixels(4000000, 4 / 3);
  assert.equal(dims.width % 8, 0);
  assert.equal(dims.height % 8, 0);
  assert.ok(Math.abs(dims.width / dims.height - 4 / 3) < 0.02);
  assert.ok(Math.abs(dims.width * dims.height - 4000000) < 4000000 * 0.03);
  const square = TOOLS.dimensionsForPixels(1000000, 1);
  assert.equal(square.width, square.height);
});

test("planEstimate validates resolution mode", () => {
  const plan = TOOLS.planEstimate({
    mode: "resolution",
    width: 1920,
    height: 1080,
    format: "jpeg",
    content: "photo",
    quality: 85,
  });
  assert.equal(plan.valid, true);
  assert.equal(plan.width, 1920);
  assert.equal(plan.pixels, 1920 * 1080);
  assert.ok(plan.estimateBytes > 0);

  const tooSmall = TOOLS.planEstimate({ mode: "resolution", width: 64, height: 64 });
  assert.equal(tooSmall.valid, false);
  assert.match(tooSmall.error, /128/);

  const tooLarge = TOOLS.planEstimate({ mode: "resolution", width: 20000, height: 20000 });
  assert.equal(tooLarge.valid, false);

  const pngOver = TOOLS.planEstimate({
    mode: "resolution",
    width: 8000,
    height: 8000,
    format: "png",
  });
  assert.equal(pngOver.valid, false);
  assert.match(pngOver.error, /PNG/);
});

test("planEstimate estimates dimensions for byte targets", () => {
  const plan = TOOLS.planEstimate({
    mode: "bytes",
    targetBytes: 1024 * 1024,
    format: "jpeg",
    content: "photo",
    quality: 85,
    aspect: 4 / 3,
  });
  assert.equal(plan.valid, true);
  assert.equal(plan.targetBytes, 1024 * 1024);
  assert.equal(plan.clamped, false);
  assert.ok(Math.abs(plan.width / plan.height - 4 / 3) < 0.02);

  const tooSmall = TOOLS.planEstimate({ mode: "bytes", targetBytes: 512 });
  assert.equal(tooSmall.valid, false);

  const tooBig = TOOLS.planEstimate({
    mode: "bytes",
    targetBytes: TOOLS.MAX_OUTPUT_BYTES + 1,
  });
  assert.equal(tooBig.valid, false);
});

test("planEstimate clamps byte targets above the format pixel limit", () => {
  const plan = TOOLS.planEstimate({
    mode: "bytes",
    targetBytes: 256 * TOOLS.MIB,
    format: "png",
    content: "noise",
  });
  assert.equal(plan.valid, true);
  assert.equal(plan.clamped, true);
  assert.match(plan.note, /未收敛/);
  assert.ok(plan.pixels <= 25000000);
});

test("planEstimate warns when WebP is above 25 MP", () => {
  const webp = TOOLS.planEstimate({
    mode: "resolution",
    width: 8000,
    height: 6000,
    format: "webp",
  });
  assert.equal(webp.valid, true);
  assert.match(webp.note, /WebP 超过 25 MP/);

  const jpeg = TOOLS.planEstimate({
    mode: "resolution",
    width: 8000,
    height: 6000,
    format: "jpeg",
  });
  assert.equal(jpeg.note, "");

  const webpBytes = TOOLS.planEstimate({
    mode: "bytes",
    targetBytes: 8 * TOOLS.MIB,
    format: "webp",
    content: "photo",
  });
  assert.equal(webpBytes.valid, true);
  assert.match(webpBytes.note, /WebP 超过 25 MP/);
});

test("parseTargetBytes defaults to MiB and accepts units", () => {
  assert.equal(TOOLS.parseTargetBytes("1"), TOOLS.MIB);
  assert.equal(TOOLS.parseTargetBytes("1.5 MiB"), Math.round(1.5 * TOOLS.MIB));
  assert.equal(TOOLS.parseTargetBytes("1024 KiB"), 1024 * 1024);
  assert.equal(TOOLS.parseTargetBytes("500 KB"), 500000);
  assert.equal(TOOLS.parseTargetBytes("2 mb"), 2000000);
  assert.equal(TOOLS.parseTargetBytes("1,024 kib"), 1024 * 1024);
  assert.equal(TOOLS.parseTargetBytes("12 B"), 12);
  assert.equal(TOOLS.parseTargetBytes(""), null);
  assert.equal(TOOLS.parseTargetBytes("abc"), null);
  assert.equal(TOOLS.parseTargetBytes("0"), null);
  assert.equal(TOOLS.parseTargetBytes("-3 MiB"), null);
});

test("parseSpecLine accepts dimensions and byte targets", () => {
  assert.deepEqual(TOOLS.parseSpecLine("1920x1080"), {
    mode: "resolution",
    width: 1920,
    height: 1080,
  });
  assert.deepEqual(TOOLS.parseSpecLine("1920 × 1080"), {
    mode: "resolution",
    width: 1920,
    height: 1080,
  });
  assert.deepEqual(TOOLS.parseSpecLine("1920*1080"), {
    mode: "resolution",
    width: 1920,
    height: 1080,
  });
  assert.deepEqual(TOOLS.parseSpecLine("5 MiB"), {
    mode: "bytes",
    targetBytes: 5 * TOOLS.MIB,
  });
  assert.equal(TOOLS.parseSpecLine("hello"), null);
  assert.equal(TOOLS.parseSpecLine(""), null);
});

test("parseBatch collects valid and invalid lines", () => {
  const result = TOOLS.parseBatch("1920x1080\n\n5 MiB\nbad line\n2048x1536");
  assert.equal(result.specs.length, 3);
  assert.equal(result.specs[0].width, 1920);
  assert.equal(result.specs[1].targetBytes, 5 * TOOLS.MIB);
  assert.equal(result.specs[2].height, 1536);
  assert.deepEqual(result.invalid, [{ line: 4, text: "bad line" }]);
});

test("summariseSpec joins spec, format, content and probe", () => {
  const text = TOOLS.summariseSpec(
    { mode: "resolution", width: 800, height: 600 },
    { format: "jpeg", quality: 85, content: "photo", probe: true }
  );
  assert.match(text, /800×600/);
  assert.match(text, /JPEG q85/);
  assert.match(text, /照片纹理/);
  assert.match(text, /探针/);

  const png = TOOLS.summariseSpec(
    { mode: "bytes", targetBytes: 5 * TOOLS.MIB },
    { format: "png", content: "solid" }
  );
  assert.match(png, /5.00 MiB 目标/);
  assert.match(png, /PNG 无损/);
  assert.match(png, /纯色块/);
});

test("convergedRate ignores resolution-only entries", () => {
  assert.equal(TOOLS.convergedRate([]), null);
  const rate = TOOLS.convergedRate([
    { target_bytes: 100, converged: true },
    { target_bytes: 100, converged: false },
    { target_bytes: null, converged: false },
  ]);
  assert.deepEqual(rate, { total: 2, converged: 1, ratio: 0.5 });
});

test("deviationStatus buckets deviations", () => {
  assert.equal(TOOLS.deviationStatus(null), "none");
  assert.equal(TOOLS.deviationStatus(""), "none");
  assert.equal(TOOLS.deviationStatus(0.01), "converged");
  assert.equal(TOOLS.deviationStatus(-0.015), "converged");
  assert.equal(TOOLS.deviationStatus(0.03), "near");
  assert.equal(TOOLS.deviationStatus(0.2), "off");
  assert.equal(TOOLS.deviationStatus(0.01, 0), "converged");
});

test("labels fall back safely", () => {
  assert.equal(TOOLS.statusLabel("running"), "进行中");
  assert.equal(TOOLS.contentLabel("noise"), "高频噪声");
  assert.equal(TOOLS.formatLabel("webp"), "WebP");
  assert.equal(TOOLS.deviationLabel("near"), "接近目标");
  assert.equal(TOOLS.statusLabel("weird"), "weird");
});

test("escapeHtml escapes all five characters", () => {
  assert.equal(
    TOOLS.escapeHtml('<a href="x">&\'</a>'),
    "&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;"
  );
  assert.equal(TOOLS.escapeHtml(null), "");
});

test("getCookieValue finds an encoded cookie", () => {
  const cookie = "a=1; imglab.csrf=abc%3D123; b=2";
  assert.equal(TOOLS.getCookieValue(cookie, "imglab.csrf"), "abc=123");
  assert.equal(TOOLS.getCookieValue(cookie, "missing"), "");
  assert.equal(TOOLS.getCookieValue("", "x"), "");
});

test("formatTimestamp renders local time", () => {
  const stamp = TOOLS.formatTimestamp("2026-01-02T03:04:05");
  assert.equal(stamp, "2026-01-02 03:04:05");
  assert.equal(TOOLS.formatTimestamp("not a date"), "—");
  assert.equal(TOOLS.formatTimestamp(""), "—");
});

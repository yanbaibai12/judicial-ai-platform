// Run only against an isolated, empty local instance.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const base = process.env.JAI_CONSOLE_TEST_URL;
const token = process.env.JAI_CONSOLE_ADMIN_TOKEN;
const chrome = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
if (!base || !token) {
  throw new Error("Set JAI_CONSOLE_TEST_URL and JAI_CONSOLE_ADMIN_TOKEN for an isolated instance");
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "jai-console-chrome-"));
  const browser = spawn(chrome, [
    "--headless", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true });
  let socket;
  try {
    let port;
    for (let attempt = 0; attempt < 100; attempt++) {
      try {
        port = Number(fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]);
        if (port) break;
      } catch {}
      if (browser.exitCode !== null) throw new Error("Chrome exited before starting");
      await pause(100);
    }
    assert.ok(port, "Chrome debugging port unavailable");
    const target = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(base)}`, {
      method: "PUT",
    }).then((response) => response.json());
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    let sequence = 0;
    const pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id || !pending.has(message.id)) return;
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    });
    function send(method, params = {}) {
      const id = ++sequence;
      socket.send(JSON.stringify({ id, method, params }));
      return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
    }
    async function evaluate(expression) {
      const result = await send("Runtime.evaluate", { expression, returnByValue: true });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
      return result.result.value;
    }
    async function waitFor(expression) {
      for (let attempt = 0; attempt < 100; attempt++) {
        if (await evaluate(expression)) return;
        await pause(100);
      }
      throw new Error(`Timed out waiting for ${expression}`);
    }
    await send("Runtime.enable");
    await send("Network.enable");
    await waitFor("document.querySelector('#dashboardStatus') && !document.body.classList.contains('initializing')");
    await waitFor("document.querySelector('#healthLabel').textContent !== '健康状态未探测'");
    assert.equal(await evaluate("document.querySelector('#dashboardStatus').textContent"), "未连接");
    assert.equal(await evaluate("document.querySelector('#statCalls').textContent"), "—");
    assert.equal(await evaluate("document.querySelector('#retrievalResults').hidden"), true);
    await send("Emulation.setDeviceMetricsOverride", {
      width: 390, height: 844, deviceScaleFactor: 1, mobile: true,
    });
    const dimensions = await evaluate("[window.innerWidth, document.documentElement.scrollWidth]");
    assert.ok(dimensions[1] <= dimensions[0], `Mobile overflow: ${dimensions}`);
    await send("Emulation.clearDeviceMetricsOverride");

    await evaluate(`sessionStorage.setItem('jai.adminToken', ${JSON.stringify(token)}); location.reload()`);
    await waitFor("document.querySelector('#connectionLabel').textContent === '已连接'");
    assert.equal(await evaluate("document.querySelector('#statCalls').textContent"), "0");
    assert.equal(await evaluate("document.querySelector('#govFeedback').textContent"), "—");
    await send("Network.setBlockedURLs", { urls: ["*/api/v1/governance/metrics"] });
    await evaluate("document.querySelector('#refreshGovernance').click()");
    await waitFor("document.querySelector('#dashboardStatus').textContent.includes('metrics')");
    assert.equal(await evaluate("document.querySelector('#statCalls').textContent"), "—");
    assert.equal(await evaluate("document.querySelector('#connectionLabel').textContent"), "部分数据读取失败");
    await send("Network.setBlockedURLs", { urls: [] });
    await evaluate("document.querySelector('#refreshGovernance').click()");
    await waitFor("document.querySelector('#connectionLabel').textContent === '已连接'");
    assert.equal(await evaluate("document.querySelector('#statCalls').textContent"), "0");
    await evaluate("document.querySelector('#disconnectBtn').click()");
    assert.equal(await evaluate("document.querySelector('#dashboardStatus').textContent"), "未连接");
    assert.equal(await evaluate("document.querySelector('#statCalls').textContent"), "—");
    console.log("Console smoke test passed: disconnected, empty, failed, recovered, disconnected.");
  } finally {
    if (socket) socket.close();
    browser.kill();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

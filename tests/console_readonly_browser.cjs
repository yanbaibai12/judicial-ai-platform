const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const base = process.env.JAI_CONSOLE_TEST_URL;
if (!base || !["127.0.0.1", "localhost", "[::1]"].includes(new URL(base).hostname)) {
  throw new Error("A loopback console URL is required");
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "jai-readonly-chrome-"));
  const browser = spawn(process.env.CHROME_PATH ||
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", [
    "--headless", "--disable-gpu", "--no-first-run", "--remote-debugging-port=0",
    `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true });
  let socket;
  try {
    let port;
    for (let i = 0; i < 100 && !port; i++) {
      try { port = Number(fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]); } catch {}
      if (browser.exitCode !== null) throw new Error("Chrome exited before starting");
      if (!port) await pause(100);
    }
    assert.ok(port, "Chrome debugging port unavailable");
    const target = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(base)}`,
      { method: "PUT" }).then((response) => response.json());
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    let sequence = 0;
    const pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!pending.has(message.id)) return;
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : resolve(message.result);
    });
    const send = (method, params = {}) => {
      const id = ++sequence;
      socket.send(JSON.stringify({ id, method, params }));
      return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
    };
    async function evaluate(expression) {
      const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
      return result.result.value;
    }
    await send("Runtime.enable");
    for (let i = 0; i < 100; i++) {
      if (await evaluate("!!document.querySelector('#connectionLabel') && !document.body.classList.contains('initializing')")) break;
      await pause(100);
    }
    for (const route of ["capabilities", "retrieval", "governance", "settings", "overview"]) {
      await evaluate(`document.querySelector('[data-route="${route}"]').click()`);
      let visible = false;
      for (let i = 0; i < 100 && !visible; i++) {
        visible = await evaluate(`!document.querySelector('#page-${route}').hidden`);
        if (!visible) await pause(100);
      }
      assert.equal(visible, true, `${route} did not open`);
    }
    const state = await evaluate(`(async () => ({
      title: document.title,
      connection: document.querySelector('#connectionLabel').textContent,
      health: document.querySelector('#healthLabel').textContent,
      detail: document.querySelector('#healthDetail').textContent,
      readiness: await fetch('/readyz').then((response) => response.json())
    }))()`);
    console.log(JSON.stringify(state, null, 2));
    console.log("Read-only console navigation passed.");
  } finally {
    if (socket) socket.close();
    browser.kill();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });

// Mutates only an explicitly isolated local acceptance instance.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const base = process.env.JAI_CONSOLE_TEST_URL;
const admin = process.env.JAI_CONSOLE_ADMIN_TOKEN;
if (process.env.JAI_CONSOLE_TEST_ISOLATED !== "1" || !admin ||
    !base || !["localhost", "127.0.0.1", "::1"].includes(new URL(base).hostname)) {
  throw new Error("An isolated loopback instance and admin token are required");
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "jai-tenant-browser-"));
  const browser = spawn(process.env.CHROME_PATH ||
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", [
    "--headless", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true });
  let socket;
  try {
    let port;
    for (let i = 0; i < 100 && !port; i++) {
      try { port = Number(fs.readFileSync(path.join(profile, "DevToolsActivePort"), "utf8").split("\n")[0]); } catch {}
      if (browser.exitCode !== null) throw new Error("Browser exited");
      if (!port) await pause(100);
    }
    assert.ok(port, "Browser debugging port unavailable");
    const target = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(base)}`,
      { method: "PUT" }).then((response) => response.json());
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    let seq = 0;
    const pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id || !pending.has(message.id)) return;
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : resolve(message.result);
    });
    const send = (method, params = {}) => {
      const id = ++seq;
      socket.send(JSON.stringify({ id, method, params }));
      return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
    };
    async function evaluate(expression) {
      const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
      return result.result.value;
    }
    async function waitFor(expression) {
      for (let i = 0; i < 300; i++) {
        if (await evaluate(expression)) return;
        await pause(100);
      }
      throw new Error(`Timed out waiting for ${expression}`);
    }
    await send("Runtime.enable");
    await waitFor("document.querySelector('#connectionLabel') && !document.body.classList.contains('initializing')");
    await evaluate("document.querySelector('[data-route=\"settings\"]').click()");
    await evaluate("document.querySelector('[data-open-tenant-create]').click()");
    await evaluate("document.querySelector('#tenantCode').value = 'browser-tenant-test'");
    await evaluate("document.querySelector('#tenantName').value = '浏览器测试法院'");
    await evaluate("document.querySelector('#tenantCreateForm').requestSubmit()");
    assert.match(await evaluate("document.querySelector('#tenantCreateError').textContent"), /管理令牌/);
    await evaluate("document.querySelector('#tenantCreateDialog').close()");
    await evaluate("document.querySelector('[data-open-token]').click()");
    await evaluate(`document.querySelector('#adminTokenInput').value = ${JSON.stringify(admin)}`);
    await evaluate("document.querySelector('#tokenForm').requestSubmit()");
    await waitFor("document.querySelector('#connectionLabel').textContent === '已连接'");
    await evaluate("document.querySelector('[data-open-tenant-create]').click()");
    await evaluate("document.querySelector('#tenantCode').value = 'browser-tenant-test'");
    await evaluate("document.querySelector('#tenantName').value = '浏览器测试法院'");
    await evaluate("document.querySelector('#tenantCreateForm').requestSubmit()");
    await waitFor("!document.querySelector('#tenantCreateDialog').open");
    await waitFor("document.querySelector('#toast').textContent.includes('租户已创建')");
    await evaluate("document.querySelector('[data-route=\"retrieval\"]').click()");
    await evaluate("document.querySelector('[data-open-kb-create]').click()");
    assert.match(await evaluate("document.querySelector('#kbTenant').textContent"), /浏览器测试法院/);
    await evaluate("document.querySelector('#kbCreateDialog').close()");
    await evaluate("document.querySelector('[data-route=\"settings\"]').click()");
    await evaluate("document.querySelector('[data-open-tenant-create]').click()");
    await evaluate("document.querySelector('#tenantCode').value = 'browser-tenant-test'");
    await evaluate("document.querySelector('#tenantName').value = '重复租户'");
    await evaluate("document.querySelector('#tenantCreateForm').requestSubmit()");
    await waitFor("!document.querySelector('#tenantCreateError').hidden");
    assert.match(await evaluate("document.querySelector('#tenantCreateError').textContent"), /已存在/);
    console.log("Tenant creation, authorization, refresh and duplicate handling verified in browser.");
  } finally {
    if (socket) socket.close();
    browser.kill();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });

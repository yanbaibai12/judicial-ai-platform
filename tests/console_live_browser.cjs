// Run against an explicitly configured isolated instance with real dependencies.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const base = process.env.JAI_CONSOLE_TEST_URL;
const admin = process.env.JAI_CONSOLE_ADMIN_TOKEN;
const app = process.env.JAI_CONSOLE_APP_TOKEN;
const kbId = process.env.JAI_CONSOLE_KB_ID;
const documentId = process.env.JAI_CONSOLE_DOC_ID;
const appId = process.env.JAI_CONSOLE_APP_ID;
const chrome = process.env.CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
if (!base || !admin || !app || !kbId || !documentId || !appId) {
  throw new Error("Missing isolated instance configuration");
}
if (process.env.JAI_CONSOLE_TEST_ISOLATED !== "1" ||
    !["127.0.0.1", "localhost", "::1"].includes(new URL(base).hostname)) {
  throw new Error("This mutating test requires an explicitly isolated loopback instance");
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "jai-live-chrome-"));
  const browser = spawn(chrome, [
    "--headless", "--disable-gpu", "--no-first-run",
    "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank",
  ], { stdio: "ignore", windowsHide: true });
  let socket;
  try {
    let port;
    for (let i = 0; i < 100; i++) {
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
    let seq = 0;
    const pending = new Map();
    const requests = [];
    const grantRequests = [];
    const manualIndexRequests = [];
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.method === "Network.requestWillBeSent" &&
          message.params.request.url.endsWith("/v1/retrieve")) {
        requests.push(message.params.request.postData);
      }
      if (message.method === "Network.requestWillBeSent" &&
          message.params.request.url.includes("/grants") &&
          message.params.request.method === "POST") {
        grantRequests.push(message.params.request.postData);
      }
      if (message.method === "Network.requestWillBeSent" &&
          message.params.request.url.includes("/index-jobs") &&
          message.params.request.method === "POST") {
        manualIndexRequests.push(message.params.request.url);
      }
      if (!message.id || !pending.has(message.id)) return;
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    });
    function send(method, params = {}) {
      const id = ++seq;
      socket.send(JSON.stringify({ id, method, params }));
      return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
    }
    async function evaluate(expression) {
      const result = await send("Runtime.evaluate", {
        expression, returnByValue: true, awaitPromise: true,
      });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
      return result.result.value;
    }
    async function waitFor(expression) {
      for (let i = 0; i < 200; i++) {
        if (await evaluate(expression)) return;
        await pause(100);
      }
      throw new Error(`Timed out waiting for ${expression}`);
    }
    await send("Runtime.enable");
    await send("Network.enable");
    await waitFor("document.querySelector('#connectionLabel') && !document.body.classList.contains('initializing')");
    assert.equal(await evaluate("document.querySelector('#connectionLabel').textContent"), "未连接");
    await evaluate("document.querySelector('[data-open-token]').click()");
    assert.equal(await evaluate("document.querySelector('#tokenDialog').open"), true);
    await evaluate(`document.querySelector('#adminTokenInput').value = ${JSON.stringify(admin)}`);
    await evaluate(`document.querySelector('#appTokenInput').value = ${JSON.stringify(app)}`);
    await evaluate("document.querySelector('#tokenForm').requestSubmit()");
    await waitFor("document.querySelector('#connectionLabel').textContent === '已连接'");
    await waitFor("document.querySelector('#healthLabel').textContent !== '健康状态未探测'");
    assert.equal(await evaluate("document.querySelector('#healthLabel').textContent"), "服务未就绪");
    assert.match(await evaluate("document.querySelector('#healthDetail').textContent"), /inference/);
    assert.match(await evaluate("document.querySelector('#capabilityStrip').textContent"), /公开司法资料检索/);
    await evaluate("document.querySelector('[data-route=\"capabilities\"]').click()");
    await evaluate("document.querySelector('[data-open-capability]').click()");
    await evaluate("document.querySelector('[data-grant-capability]').click()");
    await waitFor("document.querySelector('#grantDialog').open");
    await evaluate("document.querySelector('#grantApp').selectedIndex = 1");
    await evaluate("document.querySelector('#grantKbIds').value = 'not-an-id'");
    await evaluate("document.querySelector('#grantForm').requestSubmit()");
    assert.match(await evaluate("document.querySelector('#grantError').textContent"), /正整数/);
    assert.equal(grantRequests.length, 0);
    await evaluate("document.querySelector('#grantDialog').close()");
    for (const route of ["capabilities", "retrieval", "governance", "settings", "overview", "retrieval"]) {
      await evaluate(`document.querySelector('[data-route="${route}"]').click()`);
      assert.equal(await evaluate(`document.querySelector('#page-${route}').hidden`), false);
    }
    assert.match(await evaluate("document.querySelector('#kbList').textContent"), /4 文档/);
    assert.equal(await evaluate("document.querySelector('#kbChips .is-selected').dataset.kbId"), kbId);
    await evaluate(`document.querySelectorAll('#kbChips .is-selected').forEach((chip) => {
      if (chip.dataset.kbId !== ${JSON.stringify(kbId)}) chip.click();
    })`);
    await evaluate("document.querySelector('#retrievalQuery').value = '生态环境法典时间效力'");
    await evaluate("document.querySelector('#retrievalForm').requestSubmit()");
    await waitFor("!document.querySelector('#runRetrieval').disabled");
    assert.equal(requests.length, 1);
    assert.deepEqual(JSON.parse(requests[0]).kb_ids, [kbId]);
    assert.equal(await evaluate("document.querySelector('#retrievalResults').hidden"), true);
    const toast = await evaluate("document.querySelector('#toast').textContent");
    assert.match(toast, /模型服务拒绝请求|模型服务不可用|模型服务连接失败/);
    await evaluate("document.querySelector('[data-open-document]').click()");
    assert.equal(await evaluate("document.querySelector('#documentDialog').open"), true);
    await evaluate(`document.querySelector('#documentKb').value = ${JSON.stringify(kbId)}`);
    await evaluate("document.querySelector('#documentTitleInput').value = '浏览器隔离验收资料'");
    await evaluate(`document.querySelector('#documentText').value = ${JSON.stringify(
      `生态环境法典施行后的法律事实引起的生态环境案件，适用生态环境法典。\n验收标识 ${Date.now()}`
    )}`);
    await evaluate("document.querySelector('#documentForm').requestSubmit()");
    await waitFor("!document.querySelector('#documentError').hidden");
    assert.match(
      await evaluate("document.querySelector('#documentError').textContent"),
      /模型服务拒绝请求|模型服务不可用|模型服务连接失败/,
    );
    assert.equal(await evaluate("document.querySelector('#documentDialog').open"), true);
    await evaluate(`(async () => {
      const response = await fetch('/api/v1/documents/${encodeURIComponent(documentId)}/download', {
        headers: { Authorization: 'Bearer ' + ${JSON.stringify(admin)} }
      });
      if (!response.ok) throw new Error('Cannot read isolated source document');
      document.querySelector('#documentText').value = await response.text();
    })()`);
    await evaluate("document.querySelector('#documentForm').requestSubmit()");
    await waitFor("!document.querySelector('#documentDialog').open");
    await waitFor("document.querySelector('#toast').textContent.includes('文档已入库')");
    assert.match(
      await evaluate("document.querySelector('#toast').textContent"),
      /文档已入库，索引最近一次执行失败/,
    );
    assert.equal(manualIndexRequests.length, 0);
    const unique = Date.now().toString();
    const capabilityCode = `browser.test-${unique}`;
    await evaluate("document.querySelector('[data-open-kb-create]').click()");
    await evaluate(`document.querySelector('#kbCode').value = 'browser-${unique}'`);
    await evaluate("document.querySelector('#kbName').value = '隔离浏览器验收知识库'");
    await evaluate("document.querySelector('#kbCreateForm').requestSubmit()");
    await waitFor("!document.querySelector('#kbCreateDialog').open");
    await waitFor("document.querySelector('#kbList').textContent.includes('隔离浏览器验收知识库')");
    await evaluate("document.querySelector('[data-route=\"capabilities\"]').click()");
    await evaluate("document.querySelector('[data-open-capability-create]').click()");
    await evaluate(`document.querySelector('#capCode').value = ${JSON.stringify(capabilityCode)}`);
    await evaluate("document.querySelector('#capName').value = '隔离浏览器验收检索'");
    await evaluate("document.querySelector('#capabilityCreateForm').requestSubmit()");
    await waitFor("!document.querySelector('#capabilityCreateDialog').open");
    await waitFor(`!!document.querySelector('[data-open-capability="${capabilityCode}"]')`);
    await evaluate(`document.querySelector('[data-open-capability="${capabilityCode}"]').click()`);
    await evaluate("document.querySelector('[data-grant-capability]').click()");
    await waitFor("document.querySelector('#grantDialog').open");
    await evaluate(`document.querySelector('#grantApp').value = ${JSON.stringify(appId)}`);
    await evaluate(`document.querySelector('#grantKbIds').value = ${JSON.stringify(kbId)}`);
    await evaluate("document.querySelector('#grantForm').requestSubmit()");
    await waitFor("document.querySelector('#toast').textContent.includes('授权已生效')");
    assert.equal(grantRequests.length, 1);
    const grant = JSON.parse(grantRequests[0]);
    assert.equal(grant.app_id, appId);
    assert.deepEqual(grant.data_scope.kb_ids, [kbId]);
    await evaluate("document.querySelector('#grantDialog').close()");
    await send("Emulation.setDeviceMetricsOverride", {
      width: 390, height: 844, deviceScaleFactor: 1, mobile: true,
    });
    const dimensions = await evaluate("[window.innerWidth, document.documentElement.scrollWidth]");
    assert.ok(dimensions[1] <= dimensions[0], `Mobile overflow: ${dimensions}`);
    console.log("Real Chrome: navigation, isolated CRUD/grant, exact IDs and proxy failure verified.");
  } finally {
    if (socket) socket.close();
    browser.kill();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

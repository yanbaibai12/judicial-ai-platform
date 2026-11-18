const assert = require("node:assert/strict");
const test = require("node:test");
const waitJob = require("./console_acceptance_job.cjs");

test("waits for an asynchronous job and forwards its credentials", async () => {
  const states = ["QUEUED", "SUCCEEDED"];
  const result = await waitJob(async (method, url, body, token) => {
    assert.equal(method, "GET");
    assert.equal(url, "/api/v1/jobs/123");
    assert.equal(token, "test-token");
    return { data: { status: states.shift() } };
  }, "123", "test-token");
  assert.equal(result.status, "SUCCEEDED");
});

test("reports an unsuccessful job", async () => {
  await assert.rejects(
    waitJob(async () => ({ data: { status: "FAILED" } }), "123", "test-token"),
    /job failed: FAILED/,
  );
});

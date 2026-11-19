async function waitJob(call, id, token) {
  const deadline = Date.now() + 300000;
  while (Date.now() < deadline) {
    const job = (await call("GET", "/api/v1/jobs/" + id, undefined, token)).data;
    if (job.status === "SUCCEEDED") return job;
    if (["FAILED", "CANCELLED", "DEAD"].includes(job.status))
      throw new Error("job failed: " + job.status);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error("job timed out: " + id);
}

module.exports = waitJob;

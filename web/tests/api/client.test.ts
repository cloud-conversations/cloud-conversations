import { afterEach, describe, expect, it, vi } from "vitest";
import { approve, fetchThread, postMessage } from "../../src/api/client";

function respondWith(status: number, body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    json: async () => body,
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

/** A client that has not sent the browser to log in yet. */
async function freshClient() {
  vi.resetModules();
  return import("../../src/api/client");
}

describe("the api client", () => {
  it("sends the browser to log in, to come back where it was, on a 401", async () => {
    const client = await freshClient();
    respondWith(401, { detail: "log in first" });
    const assign = vi.fn();
    vi.stubGlobal("location", { pathname: "/threads/t1", search: "?x=1", assign });

    await expect(client.fetchThread("t1")).rejects.toThrow("log in first");

    expect(assign).toHaveBeenCalledWith("/login?next=%2Fthreads%2Ft1%3Fx%3D1");
  });

  it("sends the browser to log in once, however many calls are refused", async () => {
    const client = await freshClient();
    respondWith(401, { detail: "log in first" });
    const assign = vi.fn();
    vi.stubGlobal("location", { pathname: "/", search: "", assign });

    await Promise.allSettled([client.fetchThreads(), client.fetchMe()]);

    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("escapes an id so it cannot reach another endpoint", async () => {
    const fetchMock = respondWith(200, {});

    await fetchThread("../runs/r1");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/threads/..%2Fruns%2Fr1");
  });

  it("sends a message as json", async () => {
    const fetchMock = respondWith(202, { message: {}, job: {} });

    await postMessage("t1", "an A100 for a day");

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/threads/t1/messages");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ text: "an A100 for a day" }));
    expect(init.headers["Content-Type"]).toBe("application/json");
  });

  it("carries the reason a refusal gives", async () => {
    respondWith(409, { detail: "Thread t1 already has an unfinished build job" });

    await expect(approve("t1", "d1", 3)).rejects.toThrow(
      "Thread t1 already has an unfinished build job",
    );
  });

  it("reports the status when a refusal is not json", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => {
          throw new SyntaxError("Unexpected token '<'");
        },
      }),
    );

    await expect(approve("t1", "d1", 3)).rejects.toThrow(/^API 500$/);
  });
});

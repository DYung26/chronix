import { describe, expect, it, vi } from "vitest";
import { fetchActiveTasks } from "./chronix-client";

const payload = {
  ok: true,
  serverTime: "2026-11-16T03:30:00.000Z",
  pollAfterMs: 10_000,
  tasks: [
    {
      id: "task-1",
      title: "Implement widget API",
      startAt: "2026-11-16T03:00:00.000Z",
      endAt: "2026-11-16T04:00:00.000Z",
      track: "secondary",
    },
  ],
};

describe("fetchActiveTasks", () => {
  it("requests the active-task endpoint and parses the response", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const result = await fetchActiveTasks({
      baseUrl: "https://chronix.example.test/api-prefix/",
      fetcher,
    });

    expect(fetcher).toHaveBeenCalledWith(
      "https://chronix.example.test/api-prefix/api/v1/active-tasks",
      expect.objectContaining({
        method: "GET",
        headers: { Accept: "application/json" },
      }),
    );
    expect(result).toEqual({
      serverTime: payload.serverTime,
      tasks: payload.tasks,
      pollAfterMs: 10_000,
    });
  });

  it("rejects non-success HTTP responses", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("", { status: 503 }),
    );

    await expect(fetchActiveTasks({ fetcher })).rejects.toThrow(
      "Chronix API returned HTTP 503",
    );
  });

  it("rejects malformed payloads", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ ok: true, serverTime: "bad", tasks: [] }), {
        status: 200,
      }),
    );

    await expect(fetchActiveTasks({ fetcher })).rejects.toThrow(
      "invalid active-task payload",
    );
  });

  it("falls back to the default poll interval when the API value is invalid", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({ ...payload, pollAfterMs: 10 }),
        { status: 200 },
      ),
    );

    const result = await fetchActiveTasks({ fetcher });

    expect(result.pollAfterMs).toBe(10_000);
  });
});

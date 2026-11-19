import { describe, expect, it } from "vitest";
import {
  getTaskProgress,
  isTaskActive,
  type ActiveTask,
} from "./active-task";

const task: ActiveTask = {
  id: "task-1",
  title: "Example task",
  startAt: "2026-11-15T10:00:00.000Z",
  endAt: "2026-11-15T11:00:00.000Z",
};

const start = Date.parse(task.startAt);
const end = Date.parse(task.endAt);

describe("getTaskProgress", () => {
  it("starts full at the task start time", () => {
    expect(getTaskProgress(task, start)).toBe(1);
  });

  it("depletes linearly over the task interval", () => {
    expect(getTaskProgress(task, start + (end - start) / 2)).toBe(0.5);
  });

  it("is empty at and after the task end time", () => {
    expect(getTaskProgress(task, end)).toBe(0);
    expect(getTaskProgress(task, end + 60_000)).toBe(0);
  });

  it("remains full before the task starts", () => {
    expect(getTaskProgress(task, start - 60_000)).toBe(1);
  });

  it("returns zero for invalid or non-positive intervals", () => {
    expect(getTaskProgress({ ...task, startAt: "invalid" }, start)).toBe(0);
    expect(getTaskProgress({ ...task, endAt: task.startAt }, start)).toBe(0);
  });
});

describe("isTaskActive", () => {
  it("includes the start and excludes the end", () => {
    expect(isTaskActive(task, start)).toBe(true);
    expect(isTaskActive(task, end - 1)).toBe(true);
    expect(isTaskActive(task, end)).toBe(false);
  });

  it("excludes future, expired, and invalid intervals", () => {
    expect(isTaskActive(task, start - 1)).toBe(false);
    expect(isTaskActive(task, end + 1)).toBe(false);
    expect(isTaskActive({ ...task, startAt: "invalid" }, start)).toBe(false);
  });
});

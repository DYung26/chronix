import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  getTaskRingsSize,
  getTaskRingsWindowSize,
  TaskRings,
} from "./TaskRings";
import type { ActiveTask } from "../domain/active-task";

const tasks: ActiveTask[] = Array.from({ length: 3 }, (_, index) => ({
  id: `task-${index + 1}`,
  title: `Task ${index + 1}`,
  startAt: "2026-11-15T10:00:00.000Z",
  endAt: "2026-11-15T11:00:00.000Z",
}));

describe("TaskRings", () => {
  it("renders one ring per task and keeps the centre count exact", () => {
    const markup = renderToStaticMarkup(
      <TaskRings tasks={tasks} now={Date.parse("2026-11-15T10:30:00.000Z")} />,
    );

    expect(markup).toContain(`aria-label=\"3 active tasks\"`);
    expect(markup.match(/class="task-rings__progress"/g)).toHaveLength(3);
    expect(markup).toContain(">3</text>");
  });

  it("renders the zero state without adding a ring", () => {
    const markup = renderToStaticMarkup(<TaskRings tasks={[]} now={0} />);

    expect(markup).toContain(`aria-label=\"0 active tasks\"`);
    expect(markup).not.toContain("task-rings__progress");
    expect(markup).toContain(">0</text>");
  });
});


describe("task ring sizing", () => {
  it("grows the SVG for every additional task without a ring cap", () => {
    expect(getTaskRingsSize(1)).toBe(64);
    expect(getTaskRingsSize(2)).toBe(78);
    expect(getTaskRingsSize(3)).toBe(92);
    expect(getTaskRingsSize(20)).toBeGreaterThan(getTaskRingsSize(4));
  });

  it("keeps the desktop window at its base size until the rings need more room", () => {
    expect(getTaskRingsWindowSize(0)).toBe(90);
    expect(getTaskRingsWindowSize(3)).toBe(92);
    expect(getTaskRingsWindowSize(4)).toBe(106);
  });
});

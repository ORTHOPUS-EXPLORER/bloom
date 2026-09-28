/**
 * @vitest-environment jsdom
 */
import type { ConfigurationBundle, RuntimeStopState } from "@bloom/api-client";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import explorerManagerConfiguration from "../../../../../backend/seed/applications/explorer-manager.json";
import { App } from "../App";
import { openRuntimeApp } from "../test-support/open-runtime-app";
import type { RuntimeActionClient } from "./runtime-action-dispatcher";

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverMock as never;

const running: RuntimeStopState = { asserted: false, detail: "", engaged_at: "", stopped: false };
const stopped: RuntimeStopState = { asserted: true, detail: "", engaged_at: "", stopped: true };

function configurationClient(showStop: boolean) {
  const bundle = structuredClone(explorerManagerConfiguration) as unknown as ConfigurationBundle;
  for (const application of bundle.applications) {
    application.runtime_policy = { ...application.runtime_policy, show_stop: showStop };
  }
  return {
    listConfigurations: vi.fn(async () => ["explorer-manager"]),
    getConfiguration: vi.fn(async (): Promise<ConfigurationBundle> => structuredClone(bundle)),
    upsertConfiguration: vi.fn(async (_id: string, next: ConfigurationBundle) => structuredClone(next)),
    upsertApplication: vi.fn(async (): Promise<ConfigurationBundle> => structuredClone(bundle)),
    deleteApplication: vi.fn(async (): Promise<ConfigurationBundle> => structuredClone(bundle)),
  } as never;
}

async function openExplorer(showStop: boolean, latch: RuntimeStopState) {
  const client = {
    engageRuntimeStop: vi.fn(async () => stopped),
    getRuntimeStopState: vi.fn(async () => latch),
    publishRosTopic: vi.fn(),
  } satisfies RuntimeActionClient;
  render(<App configurationClient={configurationClient(showStop)} runtimeActionClient={client} />);
  fireEvent.click(await screen.findByRole("button", { name: "Runtime: Operate and inspect" }));
  await openRuntimeApp("Explorer Manager");
  return client;
}

describe("an app that hides STOP", () => {
  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", "/");
    window.localStorage.clear();
  });

  it("draws no STOP while the robot runs", async () => {
    const client = await openExplorer(false, running);
    // An absence proves nothing until the latch mirror has its answer.
    await waitFor(() => expect(client.getRuntimeStopState).toHaveBeenCalled());
    await act(async () => {});

    expect(screen.queryByRole("button", { name: "Stop the robot" })).toBeNull();
  }, 20000);

  it("still draws it once stopped from elsewhere, so the operator can resume", async () => {
    await openExplorer(false, stopped);

    expect(await screen.findByRole("button", { name: "Hold for one second to resume" })).toBeTruthy();
  }, 20000);

  it("keeps STOP for an app that does not hide it", async () => {
    await openExplorer(true, running);

    expect(await screen.findByRole("button", { name: "Stop the robot" })).toBeTruthy();
  }, 20000);
});

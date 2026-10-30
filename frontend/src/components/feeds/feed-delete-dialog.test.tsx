import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import type { Feed } from "@/lib/feeds";
import { FeedDeleteDialog } from "./feed-delete-dialog";

const onConfirm = vi.fn();
const onCancel = vi.fn();
const feed = { id: "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", name: "<b>Odd</b> name" } as Feed;

function show(busy = false) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <FeedDeleteDialog feed={feed} busy={busy} onConfirm={onConfirm} onCancel={onCancel} />
    </NextIntlClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the question before deleting a feed", () => {
  it("puts focus on Cancel, the safe answer", () => {
    show();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: en.Feeds.deleteDialog.cancel }),
    );
  });

  it("names the feed as text", () => {
    const { container } = show();
    expect(container.textContent).toContain("<b>Odd</b> name");
    expect(container.querySelector("b")).toBeNull();
  });

  it("confirms and cancels", () => {
    show();
    fireEvent.click(screen.getByRole("button", { name: en.Feeds.deleteDialog.confirm }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: en.Feeds.deleteDialog.cancel }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("locks both answers while it is sent", () => {
    show(true);
    const buttons = screen.getAllByRole("button") as HTMLButtonElement[];
    expect(buttons.every((button) => button.disabled)).toBe(true);
  });
});

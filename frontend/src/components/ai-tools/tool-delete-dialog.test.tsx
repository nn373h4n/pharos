import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import type { AiTool } from "@/lib/ai-tools";
import { ToolDeleteDialog } from "./tool-delete-dialog";

const tool: AiTool = {
  id: "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a01",
  name: "<b>Assistant</b>",
  url: "https://assistant.example.org/",
};
const onConfirm = vi.fn();
const onCancel = vi.fn();

function show(busy = false) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <ToolDeleteDialog tool={tool} busy={busy} onConfirm={onConfirm} onCancel={onCancel} />
    </NextIntlClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the question before deleting a tool", () => {
  it("puts focus on Cancel, the safe answer", () => {
    show();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: en.AiTools.deleteDialog.cancel }),
    );
  });

  it("names the tool as text, never as markup", () => {
    const { container } = show();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<b>Assistant</b>");
  });

  it("asks for nothing until a button is pressed, and each button does one thing", () => {
    show();
    expect(onConfirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.deleteDialog.cancel }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.deleteDialog.confirm }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("locks both buttons while the deletion is on its way", () => {
    show(true);
    const cancel = screen.getByRole("button", {
      name: en.AiTools.deleteDialog.cancel,
    }) as HTMLButtonElement;
    const confirm = screen.getByRole("button", {
      name: en.AiTools.deleteDialog.deleting,
    }) as HTMLButtonElement;
    expect(cancel.disabled && confirm.disabled).toBe(true);
  });
});

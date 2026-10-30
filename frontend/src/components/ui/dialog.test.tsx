import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Dialog } from "./dialog";

const onClose = vi.fn();

function show(open: boolean) {
  return (
    <Dialog open={open} onClose={onClose} titleId="t">
      <h2 id="t">Title</h2>
    </Dialog>
  );
}

const dialog = () => document.querySelector("dialog") as HTMLDialogElement;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Dialog", () => {
  it("opens and closes with the prop, and builds its content only while open", () => {
    const { rerender } = render(show(false));
    expect(dialog().open).toBe(false);
    expect(dialog().textContent).toBe("");
    rerender(show(true));
    expect(dialog().open).toBe(true);
    expect(dialog().textContent).toBe("Title");
    rerender(show(false));
    expect(dialog().open).toBe(false);
  });

  it("leaves closing on Escape to the parent", () => {
    render(show(true));
    const event = new Event("cancel", { cancelable: true });
    fireEvent(dialog(), event);
    expect(event.defaultPrevented).toBe(true);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(dialog().open).toBe(true);
  });

  it("comes back when the browser closed it while the parent still wants it open", () => {
    render(show(true));
    dialog().removeAttribute("open"); // what a second Escape does in a browser
    fireEvent(dialog(), new Event("close"));
    expect(dialog().open).toBe(true);
  });

  it("stays closed when the parent closed it", () => {
    const { rerender } = render(show(true));
    rerender(show(false));
    fireEvent(dialog(), new Event("close"));
    expect(dialog().open).toBe(false);
  });
});

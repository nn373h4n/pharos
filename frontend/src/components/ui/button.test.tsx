import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Button } from "./button";

afterEach(cleanup);

describe("Button", () => {
  it("renders a real button with an accessible name", () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" }).tagName).toBe("BUTTON");
  });

  it("renders its child element instead when asChild is set, so links keep link semantics", () => {
    render(
      <Button asChild>
        <a href="https://example.test/docs">Docs</a>
      </Button>,
    );
    const link = screen.getByRole("link", { name: "Docs" });
    expect(link.getAttribute("href")).toBe("https://example.test/docs");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("applies variant classes and lets callers extend them", () => {
    render(
      <Button variant="outline" className="mt-6">
        Cancel
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Cancel" });
    expect(button.className).toContain("border");
    expect(button.className).toContain("mt-6");
  });

  it("can be disabled", () => {
    render(<Button disabled>Wait</Button>);
    expect((screen.getByRole("button", { name: "Wait" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

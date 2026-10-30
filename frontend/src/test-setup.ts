// jsdom has no modal dialogs. This is just enough for the unit tests to open and close one; what a
// browser really does with it (trapping focus, closing on Escape, giving focus back) is tested
// with Playwright.

if (typeof HTMLDialogElement !== "undefined") {
  HTMLDialogElement.prototype.showModal ??= function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close ??= function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
  };
}

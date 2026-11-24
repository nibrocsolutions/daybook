function setNav(open) {
  document.body.classList.toggle("nav-open", open);
  const toggle = document.querySelector("[data-nav-toggle]");
  if (toggle) {
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  }
}

document.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-nav-toggle]");
  if (toggle) {
    setNav(!document.body.classList.contains("nav-open"));
    return;
  }
  if (event.target.closest("[data-nav-close], .sidebar a")) {
    setNav(false);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    setNav(false);
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) {
    return;
  }
  const message = form.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

document.querySelectorAll("[data-print]").forEach((button) => {
  button.addEventListener("click", () => window.print());
});

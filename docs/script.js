const root = document.documentElement;
const header = document.querySelector("[data-header]");
const navToggle = document.querySelector(".nav-toggle");
const nav = document.querySelector(".site-nav");
const themeToggle = document.querySelector("[data-theme-toggle]");

function updateHeader() {
  header?.classList.toggle("scrolled", window.scrollY > 10);
}

function setTheme(theme) {
  root.dataset.theme = theme;
  localStorage.setItem("daino-docs-theme", theme);
  themeToggle?.setAttribute(
    "aria-label",
    theme === "dark" ? "Switch to light theme" : "Switch to dark theme",
  );
  document.querySelector('meta[name="theme-color"]')?.setAttribute(
    "content",
    theme === "dark" ? "#07110d" : "#f5f8f4",
  );
}

setTheme(localStorage.getItem("daino-docs-theme") === "light" ? "light" : "dark");
updateHeader();
window.addEventListener("scroll", updateHeader, { passive: true });

themeToggle?.addEventListener("click", () => {
  setTheme(root.dataset.theme === "dark" ? "light" : "dark");
});

navToggle?.addEventListener("click", () => {
  const open = nav?.classList.toggle("open") ?? false;
  navToggle.setAttribute("aria-expanded", String(open));
});

nav?.querySelectorAll("a").forEach((link) => {
  link.addEventListener("click", () => {
    nav.classList.remove("open");
    navToggle?.setAttribute("aria-expanded", "false");
  });
});

document.querySelectorAll("[data-tabs]").forEach((tabs) => {
  const buttons = [...tabs.querySelectorAll('[role="tab"]')];
  const panels = [...tabs.querySelectorAll('[role="tabpanel"]')];

  function activate(selected) {
    buttons.forEach((button) => {
      const active = button === selected;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    panels.forEach((panel) => {
      panel.hidden = panel.id !== selected.getAttribute("aria-controls");
    });
  }

  buttons.forEach((button, index) => {
    button.addEventListener("click", () => activate(button));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % buttons.length;
      if (event.key === "ArrowLeft") next = (index - 1 + buttons.length) % buttons.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = buttons.length - 1;
      activate(buttons[next]);
      buttons[next].focus();
    });
  });

  activate(buttons.find((button) => button.getAttribute("aria-selected") === "true") ?? buttons[0]);
});

async function copyText(value, button) {
  const previous = button.textContent;
  try {
    await navigator.clipboard.writeText(value.replaceAll("\\n", "\n"));
    if (button.classList.contains("icon-copy")) {
      button.setAttribute("aria-label", "Copied");
      button.style.color = "var(--green)";
    } else {
      button.textContent = "Copied";
    }
    window.setTimeout(() => {
      if (button.classList.contains("icon-copy")) {
        button.setAttribute("aria-label", "Copy clone command");
        button.style.color = "";
      } else {
        button.textContent = previous;
      }
    }, 1400);
  } catch {
    if (!button.classList.contains("icon-copy")) button.textContent = "Select & copy";
  }
}

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", () => copyText(button.dataset.copy, button));
});

document.querySelectorAll("[data-copy-target]").forEach((button) => {
  button.addEventListener("click", () => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (target) copyText(target.innerText, button);
  });
});

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const finePointer = window.matchMedia("(pointer: fine)").matches;

/* Mobile nav toggle */
const toggle = document.querySelector(".nav-toggle");
const links = document.querySelector(".nav-links");

if (toggle && links) {
  toggle.addEventListener("click", () => {
    links.classList.toggle("open");
  });

  links.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => links.classList.remove("open"));
  });
}

/* Scroll progress bar */
const progressBar = document.querySelector(".scroll-progress");
if (progressBar) {
  let ticking = false;
  const updateProgress = () => {
    const doc = document.documentElement;
    const scrollable = doc.scrollHeight - doc.clientHeight;
    const pct = scrollable > 0 ? (doc.scrollTop / scrollable) * 100 : 0;
    progressBar.style.width = pct + "%";
    ticking = false;
  };
  document.addEventListener(
    "scroll",
    () => {
      if (!ticking) {
        requestAnimationFrame(updateProgress);
        ticking = true;
      }
    },
    { passive: true }
  );
  updateProgress();
}

/* Cursor spotlight (fine pointers only, respects reduced motion) */
const spotlight = document.querySelector(".spotlight");
if (spotlight && finePointer && !reduceMotion) {
  let raf = null;
  window.addEventListener(
    "pointermove",
    (e) => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        spotlight.style.setProperty("--spot-x", e.clientX + "px");
        spotlight.style.setProperty("--spot-y", e.clientY + "px");
        raf = null;
      });
    },
    { passive: true }
  );
  document.body.classList.add("spotlight-on");
}

/* Magnetic buttons (fine pointers only, respects reduced motion) */
if (finePointer && !reduceMotion) {
  document.querySelectorAll(".btn").forEach((btn) => {
    btn.addEventListener("pointermove", (e) => {
      const rect = btn.getBoundingClientRect();
      const x = e.clientX - rect.left - rect.width / 2;
      const y = e.clientY - rect.top - rect.height / 2;
      btn.style.transform = `translate(${x * 0.16}px, ${y * 0.35}px)`;
    });
    btn.addEventListener("pointerleave", () => {
      btn.style.transform = "";
    });
  });
}

/* Split-flap text settle (airport-board effect for status badges) */
const FLAP_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ⇄-";

function flapText(el) {
  const final = el.textContent.trim();
  if (!final) return;
  el.setAttribute("aria-label", final);
  if (reduceMotion) return;

  const chars = final.split("");
  el.textContent = "";
  const spans = chars.map((ch) => {
    const span = document.createElement("span");
    span.setAttribute("aria-hidden", "true");
    span.textContent = ch === " " ? " " : ch;
    el.appendChild(span);
    return span;
  });

  const start = performance.now();
  const perCharDelay = 30;
  const settleDuration = 240;

  function tick(now) {
    let allDone = true;
    spans.forEach((span, i) => {
      const elapsed = now - (start + i * perCharDelay);
      if (elapsed < settleDuration) {
        allDone = false;
        if (elapsed >= 0 && chars[i] !== " ") {
          span.textContent = FLAP_CHARS[Math.floor(Math.random() * FLAP_CHARS.length)];
        }
      } else {
        span.textContent = chars[i] === " " ? " " : chars[i];
      }
    });
    if (!allDone) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

/* Count-up numbers */
function countUp(el) {
  const target = parseInt(el.dataset.target, 10);
  if (Number.isNaN(target)) return;
  if (reduceMotion) {
    el.textContent = target.toLocaleString();
    return;
  }
  const duration = 1400;
  const start = performance.now();
  function tick(now) {
    const t = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = Math.round(eased * target).toLocaleString();
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

/* Reveal on scroll, plus triggers for flap-text and count-up children */
const revealEls = document.querySelectorAll(".reveal");
const observer = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("in");
        entry.target.querySelectorAll(".flap-text").forEach(flapText);
        entry.target.querySelectorAll(".count-up").forEach(countUp);
        observer.unobserve(entry.target);
      }
    });
  },
  { threshold: 0.15 }
);

revealEls.forEach((el) => observer.observe(el));

/* Hero count-up and telemetry strip aren't inside .reveal wrappers (part of the intro sequence), trigger directly */
document.querySelectorAll(".intro .count-up").forEach((el) => {
  setTimeout(() => countUp(el), reduceMotion ? 0 : 650);
});

document.querySelectorAll(".telemetry .flap-text").forEach((el, i) => {
  setTimeout(() => flapText(el), reduceMotion ? 0 : 700 + i * 90);
});

/* Pause the SVG route-marker's SMIL animation under reduced motion */
const routeSvg = document.querySelector(".route-svg");
if (routeSvg && reduceMotion && typeof routeSvg.pauseAnimations === "function") {
  routeSvg.pauseAnimations();
}

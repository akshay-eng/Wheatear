---
name: Wheatear
description: Open-source accelerator for migrating AI agents and workflows between orchestration platforms
colors:
  logbook-black: "oklch(16% 0.012 55)"
  ledger-panel: "oklch(20% 0.014 55)"
  manifest-card: "oklch(22% 0.015 55)"
  hairline: "oklch(100% 0 0 / 8%)"
  hairline-strong: "oklch(100% 0 0 / 14%)"
  ink-primary: "oklch(96% 0.012 80)"
  ink-secondary: "oklch(74% 0.02 70)"
  ink-tertiary: "oklch(54% 0.018 70)"
  waypoint-amber: "oklch(74% 0.14 55)"
  waypoint-amber-strong: "oklch(80% 0.13 55)"
  waypoint-amber-ink: "oklch(18% 0.03 55)"
  waypoint-amber-soft: "oklch(74% 0.14 55 / 12%)"
  tideline-slate: "oklch(68% 0.045 240)"
  tideline-slate-soft: "oklch(68% 0.045 240 / 12%)"
  transit-green: "oklch(72% 0.11 150)"
  transit-green-soft: "oklch(72% 0.11 150 / 12%)"
  hold-gold: "oklch(78% 0.12 85)"
  hold-gold-soft: "oklch(78% 0.12 85 / 12%)"
typography:
  display:
    fontFamily: "Archivo, -apple-system, sans-serif"
    fontSize: "clamp(2.6rem, 1.7rem + 3.4vw, 4.6rem)"
    fontWeight: 800
    lineHeight: 1.04
    letterSpacing: "-0.03em"
  headline:
    fontFamily: "Archivo, -apple-system, sans-serif"
    fontSize: "clamp(1.7rem, 1.3rem + 1.6vw, 2.6rem)"
    fontWeight: 700
    lineHeight: 1.12
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Archivo, -apple-system, sans-serif"
    fontSize: "1.15rem"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Archivo, -apple-system, sans-serif"
    fontSize: "1rem"
    fontWeight: 450
    lineHeight: 1.65
    letterSpacing: "normal"
  label:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "0.08em"
rounded:
  sm: "4px"
  md: "8px"
  lg: "14px"
  pill: "999px"
spacing:
  xs: "8px"
  sm: "16px"
  md: "24px"
  lg: "32px"
  xl: "48px"
  xxl: "96px"
components:
  button-primary:
    backgroundColor: "{colors.waypoint-amber}"
    textColor: "{colors.waypoint-amber-ink}"
    typography: "{typography.title}"
    rounded: "{rounded.md}"
    padding: "12px 22px"
  button-primary-hover:
    backgroundColor: "{colors.waypoint-amber-strong}"
    textColor: "{colors.waypoint-amber-ink}"
    rounded: "{rounded.md}"
    padding: "12px 22px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink-primary}"
    rounded: "{rounded.md}"
    padding: "12px 22px"
---

# Design System: Wheatear

## 1. Overview

**Creative North Star: "The Migration Log"**

Wheatear's surface is a working instrument, not a brochure. The reference object is a
flight or shipping logbook crossed with a navigational chart: route lines, waypoint
coordinates, manifest stamps, departure-board precision. Every recurring visual device on
the page earns its place by doing the job a real logbook entry does — marking a route, a
status, a timestamp — rather than decorating a heading.

This is a deliberate departure from the page's first draft, which leaned on an italic
display serif, a small uppercase mono "eyebrow" repeated above every section, and a wall
of identical icon-cards. That combination is the most saturated AI-template pattern in
circulation right now (Klim-influenced editorial-typographic: italic serif headline + mono
label + ruled restraint), and it reads as generic regardless of execution quality. Wheatear
rejects it explicitly: no italic serif, no repeated section eyebrows, no card grids
standing in for content that doesn't need a card.

The system is dark, instrument-panel flat, and lets one warm, precise amber — Waypoint
Amber, drawn from the Northern Wheatear's own sandy-buff breast plumage — mark anything
that is live: a route, a status, a primary action. Everything else is a desaturated neutral.

**Key Characteristics:**
- Flight-log instrument panel, not SaaS brochure — route lines, waypoints, manifests, stamps.
- One saturated color (Waypoint Amber) used deliberately, never as ambient decoration.
- Mono type appears only on real data — coordinates, statuses, code, timestamps — never as
  a decorative section kicker.
- Repeated content (platforms, pipeline stages) reads as log rows or a true numbered
  sequence, never as a wall of identical icon-cards.
- Flat at rest; depth comes from hairline borders, grain, and state-triggered glow — not
  ambient drop shadows.

## 2. Colors

A near-black "logbook" neutral carries the page; one warm amber marks anything live.
Nothing else in the palette is saturated.

### Primary
- **Waypoint Amber** (oklch(74% 0.14 55)): The single live signal on the page — primary
  buttons, the animated route line, active data readouts, the one "Committed" color
  surface in the CTA section. Drawn directly from the Northern Wheatear's sandy-buff
  breast. Never used for more than one accent role on screen at a time.

### Secondary
- **Tideline Slate** (oklch(68% 0.045 240)): Cool counterweight to the amber, drawn from
  the bird's blue-grey back. Used for the "deterministic" half of the approach section and
  for secondary iconography — never competes with amber for primary attention.

### Neutral
- **Logbook Black** (oklch(16% 0.012 55)): Page background. A true near-black with a hair
  of the brand's own warm hue — not a cool slate, not a cream.
- **Ledger Panel** (oklch(20% 0.014 55)): Raised surfaces — nav bar, footer band.
- **Manifest Card** (oklch(22% 0.015 55)): Reserved for the few genuine card-shaped
  components (the terminal/IR snippet, the route-chart panel) — not used for repeated
  content grids.
- **Manifest Ink** (oklch(96% 0.012 80)): Primary text.
- **Ink Secondary** (oklch(74% 0.02 70)) / **Ink Tertiary** (oklch(54% 0.018 70)): Supporting
  text and metadata, in descending emphasis.
- **Hairline** (oklch(100% 0 0 / 8%)) / **Hairline Strong** (oklch(100% 0 0 / 14%)):
  Borders. The page has almost no shadows; hairlines do the separating.

### Status
- **Transit Green** (oklch(72% 0.11 150)): "Building" status on the platform manifest.
- **Hold Gold** (oklch(78% 0.12 85)): "Planned" status — a distinct hue from Waypoint
  Amber so status and brand color are never confused.

### Named Rules
**The Single Waypoint Rule.** Waypoint Amber is the only saturated color the page
contains. When it appears, it marks something live — a route, an action, a status —
never decoration. If you reach for amber on something static, stop and use a neutral.

**The No-Cream Rule.** The body background is never a warm off-white. It is Logbook Black
or one of its tonal steps, full stop.

## 3. Typography

**Display Font:** Archivo (with -apple-system, sans-serif fallback)
**Body Font:** Archivo (same family, weight/size contrast carries the hierarchy)
**Label/Mono Font:** JetBrains Mono (with ui-monospace fallback)

**Character:** One confident grotesk, pushed to its extremes — 800-weight display against
450-weight body — instead of a timid display-plus-body pairing. JetBrains Mono enters only
where the content is literally data.

### Hierarchy
- **Display** (800, clamp(2.6rem, 1.7rem + 3.4vw, 4.6rem), 1.04 line-height, -0.03em):
  Hero headline only.
- **Headline** (700, clamp(1.7rem, 1.3rem + 1.6vw, 2.6rem), 1.12): Section headings.
- **Title** (650, 1.15rem, 1.3): Card/component headings, button labels.
- **Body** (450, 1rem, 1.65 line-height): Running copy, capped at 70ch.
- **Label** (500, 0.75rem, 0.08em tracking, JetBrains Mono, uppercase): Status tags, route
  coordinates, waypoint codes — real data only.

### Named Rules
**The Data-Earns-Mono Rule.** JetBrains Mono is reserved for things that are literally
data — coordinates, status codes, YAML/IR snippets, timestamps. It never substitutes for a
section kicker or a decorative label.

**The One Kicker Rule.** At most one section on the page may carry a small label above its
heading. Every other section leads with the heading directly. This page spends its one
kicker on the hero; every other `## ` section heading stands alone.

## 4. Elevation

Flat by default. The page reads as a panel of instruments sitting at the same depth, not a
stack of floating cards. Depth is conveyed by hairline borders, the grain-texture overlay,
and short, purposeful glow that appears only in response to hover/focus or to mark "this is
live right now" (the animated route line, an active status).

### Shadow Vocabulary
- **route-glow** (`box-shadow: 0 0 32px oklch(74% 0.14 55 / 25%)`): Around the animated
  route line and the CTA's amber panel only — marks the one "live" surface per view.
- **focus-ring** (`0 0 0 2px oklch(16% 0.012 55), 0 0 0 4px oklch(74% 0.14 55)`): Keyboard
  focus on every interactive element.

### Named Rules
**The Flat-By-Default Rule.** Surfaces sit flat at rest. Shadow or glow appears only as a
response to hover, focus, or to mark something as live — never as ambient styling on a
static panel.

## 5. Components

### Buttons
- **Shape:** rounded-md (8px) — slightly sharper than a typical SaaS pill, instrument-panel
  edge rather than soft-app edge.
- **Primary:** Waypoint Amber background, Waypoint Amber Ink text, Title typography,
  12px/22px padding.
- **Hover:** background steps to Waypoint Amber Strong, 1px lift, no shadow added (the
  color shift is the feedback).
- **Ghost:** transparent background, hairline-strong border, Manifest Ink text; hover
  shifts border and text to Waypoint Amber.
- **Focus:** every button gets the focus-ring shadow token, visible on keyboard nav.

### Log Rows (replaces generic cards for repeated content)
- **Style:** full-width row, hairline border-bottom, no background fill, no individual
  border-radius — reads as one continuous manifest, not a grid of boxes.
- **Content:** label/value pairs in Body type, status tag in Label type (mono, pill-shaped,
  colored by status: Transit Green / Hold Gold).
- **Use for:** the platform manifest. Replaces the four-column card grid from the first
  draft.

### Numbered Sequence (the one legitimate use of numbering)
- **Style:** large Display-weight numerals in Ink Tertiary, Headline-weight step title,
  Body description.
- **Use for:** the five-stage pipeline only — it is a genuine ordered sequence, so the
  numbering carries real information rather than acting as section-grammar scaffolding.

### Terminal / Data Card
- **Corner Style:** rounded-lg (14px).
- **Background:** Manifest Card over Logbook Black, hairline border.
- **Chrome:** three small status dots (Tideline Slate, Hold Gold, Waypoint Amber — not the
  generic red/yellow/green) and a filename tab in Label type.
- **Content:** the real canonical IR YAML snippet, syntax-colored: keys in Ink Secondary,
  strings in Transit Green, the route fields in Waypoint Amber.
- **Use for:** the one place code appears on the page — doubles as the "data visualization"
  imagery requirement for a dev-tool brand.

### Route Chart Panel (signature component)
- **Corner Style:** rounded-lg (14px), Manifest Card background, hairline border.
- **Content:** an SVG chart rendering the actual source-platforms → IR → target-platforms
  flow as a navigational route line, with waypoint dots at each platform and a small marker
  animating along the path (`prefers-reduced-motion` swaps this to a static mid-path
  position). Coordinates and platform names render in Label type along the route.
- **Use for:** hero visual — replaces the static three-column "chips + circle icon"
  diagram from the first draft.

### Navigation
- Ledger Panel background at 78% opacity with backdrop-blur, hairline-strong bottom
  border. Links in Body type, Ink Secondary at rest, Manifest Ink on hover — no underline,
  no background pill.

## 6. Do's and Don'ts

### Do:
- **Do** use Archivo at full weight contrast (800 display against 450 body) to carry
  hierarchy from one family.
- **Do** reserve JetBrains Mono for real data — coordinates, statuses, code, timestamps.
- **Do** let Waypoint Amber mark exactly one live thing per view: a route, a primary
  action, a status.
- **Do** render repeated content (platforms) as log rows, and the pipeline as a true
  numbered sequence — both carry real information through their structure.
- **Do** keep surfaces flat at rest; reserve glow/shadow for hover, focus, and "live" state.

### Don't:
- **Don't** use Fraunces, Inter, or any italic display serif — both are named oversaturated
  AI-template defaults for this project specifically.
- **Don't** repeat a small uppercase tracked "eyebrow" above more than one section heading.
  One kicker, in the hero, full stop.
- **Don't** build a grid of identical icon-cards for the problem section or the platform
  list — that's the generic AI-template tell this redesign exists to remove.
- **Don't** use a cream, sand, or off-white body background. Logbook Black or nothing.
- **Don't** add ambient drop shadows to static panels. Flat by default; glow is earned by
  state.
- **Don't** use a colored `border-left`/`border-right` stripe as a callout accent.
- **Don't** use `background-clip: text` gradient headlines. One solid ink color; weight and
  size carry emphasis.

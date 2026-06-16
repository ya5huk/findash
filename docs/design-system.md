# Design system — the booky dashboard

The dashboard should feel like a leather-bound ledger or a page from a finance manual at an old library: serif type, parchment background, hairline rules, generous whitespace, oldstyle figures. Luxurious by restraint, not by ornament.

## Principles

- **No ornament for ornament's sake.** Hairlines and typographic hierarchy do the work. No drop shadows, no rounded corners, no gradients, no glow, no emojis, no icons.
- **Numbers tell the story.** Money columns are right-aligned, tabular oldstyle figures, generously spaced.
- **Sections, not cards.** A section is delineated by a small-caps heading and a 1px hairline, not a box.
- **Hover & affordance are subtle.** Collapsibles use the native `<details>/<summary>` element so the disclosure caret is the operating system's, not custom.
- **One column, narrow measure.** Like reading a book. Max content width ~ 880px, centered.

## Palette

| Role | Hex | Use |
|---|---|---|
| Parchment | `#f5efe3` | page background |
| Ink | `#2a2520` | body text |
| Muted ink | `#6b6357` | metadata, secondary labels |
| Hairline | `#c9bfaa` | section dividers, table borders |
| Ribbon (accent) | `#8b1a1a` | one-off accent (current-balance underline, "today" marker on charts) |
| Faded gold | `#a8924d` | tertiary accent, monogram-style flourishes |
| Positive | `#3a5a40` | gains, credits |
| Negative | `#8b1a1a` | losses, debits |

## Typography

- **Body:** `EB Garamond`, fallback `Georgia, serif`. 17px on desktop. Line height 1.55.
- **Headings:** `Cormorant Garamond`, fallback `Georgia, serif`. Small caps for section titles. Tight tracking.
- **Numbers in tables:** `font-variant-numeric: oldstyle-nums tabular-nums;` — looks period-correct and aligns cleanly.
- **Hebrew text:** rendered with the same fonts (they have Hebrew coverage); set `dir="auto"` on containers that mix English/Hebrew so RTL flows correctly.

Load fonts from Google Fonts:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,500;0,700;1,400&family=Cormorant+Garamond:ital,wght@0,500;0,700&display=swap" rel="stylesheet">
```

## Layout

- Single column, max-width 880px, centered, with 40px lateral margins on mobile.
- Top header is a small monogram-style flourish (e.g. "✦" in faded gold, or "—  Finance  —") above the date and net-worth headline.
- Each major section starts with a small-caps heading, a 1px hairline rule, and the content below.
- Tables: no vertical lines, only hairline between rows, header row in small caps muted-ink.

## Collapsibles

```html
<details open>
  <summary>Overview</summary>
  <!-- content -->
</details>
```

Style the summary so it looks like a heading; no chevron rotation animation; minimal.

## Charts (Chart.js theming)

- No grid lines (or very faint: `rgba(201, 191, 170, 0.4)`).
- No legend background, no border.
- Serif font on axes and tooltips: `EB Garamond`.
- Line charts: 1.5px lines in ink color; the benchmark (S&P 500) in muted ink dashed; deposits cumulative line in faded gold.
- Hover tooltip: parchment background, ink text, hairline border, no shadow.
- Y-axis ticks are sparse; date axis is in compact format ("Apr '26").
- No data point markers unless a single point is being highlighted.

## What to avoid

- Material Design, glassmorphism, "modern" SaaS dashboard tropes.
- Bright primary colors. Stick to the palette.
- Sans-serif anywhere except possibly monospace for the SQLite tab raw data.
- Animations beyond what's intrinsic to `<details>` (which is none, on most browsers, by design).

## Inspiration

Drop reference images into `docs/inspiration/`. Specific visual cues from your reference images (once saved) will be incorporated here.

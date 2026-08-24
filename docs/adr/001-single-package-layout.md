# ADR-001 — One installable package instead of sibling top-level folders

**Status:** Accepted · **Context:** the specification sketches `connectors/`, `analytics/`,
`agents/`, … as siblings of `core/` at the repository root.

## Problem

Top-level sibling folders read well in a diagram, but they are not importable as one distribution
without either (a) namespace-package gymnastics, (b) a `sys.path` hack, or (c) publishing eight
packages that must be version-locked to each other.

## Options

1. **Sibling top-level packages** — matches the sketch; breaks packaging, typing and entry points.
2. **A single `src/gdap/` package with the same internal boundaries** — one distribution, the same
   module names one level down.
3. **A monorepo of several distributions** — real isolation, real overhead: eight changelogs, eight
   version pins, cross-package refactors become releases.

## Decision

Option 2. `src/gdap/{connectors,analytics,ai,…}` keeps every boundary the specification asks for,
while `pip install gdap` installs one coherent thing and `gdap.connectors` is importable from a
plugin without path tricks.

## Consequences

* Module boundaries are enforced by review and by the ports in `core/ports.py`, not by packaging.
* If a subsystem ever needs an independent release cycle, it can be extracted into its own
  distribution and re-attached through the existing entry-point groups — the seam already exists.

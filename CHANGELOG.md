# Changelog

## 0.3.0

- Run after the import, not before: the import reloads the tenant defaults. The README says so.
- Restore the ports: `$ethernetX-Y` interface variables of your own snippets left without a default port
  get `ethernetX/Y` back (verified on a lab import).
- `--ui-api`: set All Firewalls' Internet Security zones to `any` through the SCM UI backend.

## 0.2.0

- `--deep`: edit the predefined snippets that pin Global (zone protection, application filters, DNS
  profile group, Internet rules, Gen-AI tags) so that Global's snippets detach and its tags delete.
  Tested step by step on a fresh tenant.
- Zone `internet`: print the manual fix (Internet Security > General Settings > Outbound Zone = any).
  With it, All Firewalls and Global end up empty.
- Errors carry SCM's detailed messages; the zone fallback re-reads the zone; no-op profile group
  edits are skipped; `GlobalProtect-Default` is kept. First full `--deep --apply` run on a fresh tenant.

## 0.1.0

- First release: plan / `--apply` / `--global`, reference-ordered deletion, fallbacks for referenced
  zones (interfaces removed) and interfaces (default port cleared), per-snippet detach, JSON backup, and
  a documented list of what SCM refuses to remove.
